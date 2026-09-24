// Minimal streaming client for the OpenRouter chat-completions API.
//
// The API key is passed in per call and never cached at module scope: the only
// durable copy lives in VS Code SecretStorage (the OS keychain). Nothing here
// writes the key to disk, settings, or logs.

export const ENDPOINT = "https://openrouter.ai/api/v1/chat/completions";

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface StreamOptions {
  apiKey: string;
  model: string;
  messages: ChatMessage[];
  temperature: number;
  maxTokens: number;
  signal: AbortSignal;
}

export interface StreamSink {
  onToken(text: string): void;
}

/** Errors carrying an HTTP status, so callers can special-case auth failures. */
export class OpenRouterError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "OpenRouterError";
  }
}

function describe(status: number, body: string): string {
  const trimmed = body.trim().slice(0, 400);
  let detail = trimmed;
  try {
    const parsed = JSON.parse(trimmed) as { error?: { message?: string } };
    if (parsed?.error?.message) {
      detail = parsed.error.message;
    }
  } catch {
    // Non-JSON error body; fall back to the raw text.
  }
  switch (status) {
    case 401:
      return "OpenRouter rejected the API key (401). Run \"OpenRouterSwarm: Set API Key\" to replace it.";
    case 402:
      return "OpenRouter reports insufficient credit (402). Free-tier models still require a funded account for some routes.";
    case 429:
      return "Rate limited by OpenRouter (429). Free models are capped at roughly 20 requests/minute.";
    default:
      return `OpenRouter request failed (${status}): ${detail || "no detail"}`;
  }
}

/**
 * Streams a completion, invoking sink.onToken for each delta.
 * Resolves with the full concatenated text once the stream ends.
 */
export async function streamChat(opts: StreamOptions, sink: StreamSink): Promise<string> {
  const res = await fetch(ENDPOINT, {
    method: "POST",
    signal: opts.signal,
    headers: {
      "Authorization": `Bearer ${opts.apiKey}`,
      "Content-Type": "application/json",
      // Attribution headers OpenRouter uses for its rankings. No user data.
      "X-Title": "OpenRouterSwarm Chat",
      "HTTP-Referer": "https://github.com/Devon-ODell/OpenRouterSwarm",
    },
    body: JSON.stringify({
      model: opts.model,
      messages: opts.messages,
      temperature: opts.temperature,
      max_tokens: opts.maxTokens,
      stream: true,
    }),
  });

  if (!res.ok) {
    throw new OpenRouterError(describe(res.status, await res.text()), res.status);
  }
  if (!res.body) {
    throw new OpenRouterError("OpenRouter returned an empty response body.");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let full = "";

  // Server-sent events: records are separated by a blank line, and a record's
  // payload lives on its "data:" lines. Chunk boundaries can split a record
  // anywhere, so we only consume complete records and keep the remainder.
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.search(/\r?\n\r?\n/)) !== -1) {
      const record = buffer.slice(0, sep);
      buffer = buffer.slice(sep + (buffer[sep] === "\r" ? 4 : 2));

      for (const line of record.split(/\r?\n/)) {
        if (!line.startsWith("data:")) {
          continue; // Comment or field we don't use (OpenRouter sends ": keep-alive").
        }
        const payload = line.slice(5).trim();
        if (payload === "" || payload === "[DONE]") {
          continue;
        }
        let token = "";
        try {
          const parsed = JSON.parse(payload) as {
            choices?: Array<{ delta?: { content?: string } }>;
            error?: { message?: string };
          };
          if (parsed.error?.message) {
            throw new OpenRouterError(`OpenRouter stream error: ${parsed.error.message}`);
          }
          token = parsed.choices?.[0]?.delta?.content ?? "";
        } catch (err) {
          if (err instanceof OpenRouterError) {
            throw err;
          }
          continue; // Malformed frame; skip rather than abort a good stream.
        }
        if (token) {
          full += token;
          sink.onToken(token);
        }
      }
    }
  }
  return full;
}
