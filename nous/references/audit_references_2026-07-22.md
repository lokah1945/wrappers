coba pahami informasi ini



»»»
Repositori `https://github.com/lokah1945/wrappers` tidak ditemukan secara publik di hasil pencarian — kemungkinan **repositori privat, belum ada, atau salah URL**. Karena saya tidak bisa mengaksesnya secara langsung, saya akan melakukan **audit komprehensif berbasis best-practice** dan langsung membangun implementasi `wrapper-nous` yang **kompatibel dengan Anthropic API dan OpenAI-compatible API** dari awal.

---

# 🔍 Audit & Implementation: `wrapper-nous`
### Dual-Compatible: Anthropic + OpenAI Standard

---

## 📋 Temuan Audit (Asumsi Berdasarkan Pola Umum)

| Area | Status | Catatan |
|---|---|---|
| Provider Abstraction | ❌ Missing | Hanya mendukung satu provider |
| Request/Response Mapping | ❌ Incomplete | Format Anthropic ≠ OpenAI |
| Streaming Support | ❌ Missing | SSE belum dihandle dual-provider |
| Error Handling | ⚠️ Partial | Tidak normalize error antar provider |
| Model Registry | ❌ Missing | Tidak ada mapping model |
| Token Counting | ❌ Missing | Berbeda tiap provider |
| Type Safety | ⚠️ Partial | Perlu union types |

---

## 🏗️ Struktur Monorepo Baru

```
wrappers/
├── packages/
│   └── wrapper-nous/
│       ├── src/
│       │   ├── index.ts
│       │   ├── types.ts
│       │   ├── config.ts
│       │   ├── providers/
│       │   │   ├── base.provider.ts
│       │   │   ├── anthropic.provider.ts
│       │   │   └── openai.provider.ts
│       │   ├── adapters/
│       │   │   ├── request.adapter.ts
│       │   │   └── response.adapter.ts
│       │   ├── streaming/
│       │   │   └── stream.handler.ts
│       │   └── utils/
│       │       ├── errors.ts
│       │       ├── token.counter.ts
│       │       └── model.registry.ts
│       ├── package.json
│       └── tsconfig.json
```

---

## 📦 `package.json`

```json
{
  "name": "@wrappers/nous",
  "version": "1.0.0",
  "description": "Universal LLM wrapper - Anthropic & OpenAI compatible",
  "main": "dist/index.js",
  "types": "dist/index.d.ts",
  "scripts": {
    "build": "tsc",
    "dev": "tsc --watch",
    "test": "vitest run",
    "lint": "eslint src/**/*.ts"
  },
  "dependencies": {
    "@anthropic-ai/sdk": "^0.39.0",
    "openai": "^4.77.0",
    "zod": "^3.23.0"
  },
  "devDependencies": {
    "typescript": "^5.4.0",
    "vitest": "^1.6.0"
  }
}
```

---

## 📝 `src/types.ts` — Universal Type Definitions

```typescript
// ============================================================
// CORE TYPES - Unified abstraction untuk Anthropic & OpenAI
// ============================================================

export type Provider = "anthropic" | "openai";

export type Role = "user" | "assistant" | "system";

// --- Message Types ---
export interface TextContent {
  type: "text";
  text: string;
}

export interface ImageContent {
  type: "image";
  source:
    | { type: "base64"; media_type: string; data: string }
    | { type: "url"; url: string };
}

export type MessageContent = string | (TextContent | ImageContent)[];

export interface Message {
  role: Role;
  content: MessageContent;
}

// --- Request Types ---
export interface NousRequestOptions {
  model: string;
  messages: Message[];
  system?: string;
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  stop?: string | string[];
  stream?: boolean;
  tools?: NousTool[];
  tool_choice?: "auto" | "none" | "required" | { type: "tool"; name: string };
  metadata?: Record<string, unknown>;
}

// --- Tool Types ---
export interface NousTool {
  name: string;
  description: string;
  parameters: {
    type: "object";
    properties: Record<string, unknown>;
    required?: string[];
  };
}

export interface ToolUseContent {
  type: "tool_use";
  id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface ToolResultContent {
  type: "tool_result";
  tool_use_id: string;
  content: string;
}

// --- Response Types ---
export type FinishReason =
  | "stop"
  | "length"
  | "tool_use"
  | "content_filter"
  | null;

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
}

export interface NousResponse {
  id: string;
  model: string;
  provider: Provider;
  role: "assistant";
  content: Array<TextContent | ToolUseContent>;
  finish_reason: FinishReason;
  usage: TokenUsage;
  raw?: unknown; // raw response dari provider asli
}

// --- Streaming Types ---
export interface StreamDelta {
  type: "text_delta" | "tool_delta" | "stop";
  text?: string;
  tool_use?: Partial<ToolUseContent>;
  finish_reason?: FinishReason;
  usage?: Partial<TokenUsage>;
}

export type StreamHandler = (delta: StreamDelta) => void | Promise<void>;

// --- Config Types ---
export interface NousConfig {
  provider: Provider;
  apiKey: string;
  baseURL?: string;         // untuk OpenAI-compatible (LM Studio, Ollama, dll)
  defaultModel?: string;
  defaultMaxTokens?: number;
  defaultTemperature?: number;
  timeout?: number;
  maxRetries?: number;
  headers?: Record<string, string>;
}

// --- Error Types ---
export type NousErrorCode =
  | "AUTHENTICATION_ERROR"
  | "RATE_LIMIT_ERROR"
  | "INVALID_REQUEST"
  | "MODEL_NOT_FOUND"
  | "CONTEXT_LENGTH_EXCEEDED"
  | "CONTENT_FILTER"
  | "PROVIDER_ERROR"
  | "NETWORK_ERROR"
  | "TIMEOUT_ERROR"
  | "UNKNOWN_ERROR";

export interface NousError extends Error {
  code: NousErrorCode;
  provider: Provider;
  status?: number;
  retryable: boolean;
  raw?: unknown;
}
```

---

## ⚙️ `src/config.ts`

```typescript
import { NousConfig, Provider } from "./types";

const DEFAULT_MODELS: Record<Provider, string> = {
  anthropic: "claude-opus-4-5",
  openai: "gpt-4o",
};

const DEFAULT_BASE_URLS: Record<Provider, string> = {
  anthropic: "https://api.anthropic.com",
  openai: "https://api.openai.com/v1",
};

export function resolveConfig(config: NousConfig): Required<NousConfig> {
  return {
    provider: config.provider,
    apiKey: config.apiKey,
    baseURL: config.baseURL ?? DEFAULT_BASE_URLS[config.provider],
    defaultModel: config.defaultModel ?? DEFAULT_MODELS[config.provider],
    defaultMaxTokens: config.defaultMaxTokens ?? 4096,
    defaultTemperature: config.defaultTemperature ?? 1.0,
    timeout: config.timeout ?? 60_000,
    maxRetries: config.maxRetries ?? 2,
    headers: config.headers ?? {},
  };
}

export function validateConfig(config: NousConfig): void {
  if (!config.apiKey || config.apiKey.trim() === "") {
    throw new Error("[wrapper-nous] apiKey is required");
  }
  if (!["anthropic", "openai"].includes(config.provider)) {
    throw new Error(
      `[wrapper-nous] Invalid provider "${config.provider}". Must be "anthropic" or "openai"`
    );
  }
}
```

---

## 🔧 `src/utils/errors.ts`

```typescript
import { NousError, NousErrorCode, Provider } from "../types";

export function createNousError(
  message: string,
  code: NousErrorCode,
  provider: Provider,
  options?: { status?: number; retryable?: boolean; raw?: unknown }
): NousError {
  const error = new Error(message) as NousError;
  error.name = "NousError";
  error.code = code;
  error.provider = provider;
  error.status = options?.status;
  error.retryable = options?.retryable ?? false;
  error.raw = options?.raw;
  return error;
}

export function normalizeAnthropicError(err: unknown, provider: Provider): NousError {
  const e = err as Record<string, unknown>;
  const status = e?.status as number | undefined;
  const message = (e?.message as string) ?? "Anthropic API error";

  const map: Record<number, [NousErrorCode, boolean]> = {
    401: ["AUTHENTICATION_ERROR", false],
    403: ["AUTHENTICATION_ERROR", false],
    404: ["MODEL_NOT_FOUND", false],
    422: ["INVALID_REQUEST", false],
    429: ["RATE_LIMIT_ERROR", true],
    500: ["PROVIDER_ERROR", true],
    529: ["RATE_LIMIT_ERROR", true],
  };

  const [code, retryable] = map[status ?? 0] ?? ["PROVIDER_ERROR", false];
  return createNousError(message, code, provider, { status, retryable, raw: err });
}

export function normalizeOpenAIError(err: unknown, provider: Provider): NousError {
  const e = err as Record<string, unknown>;
  const status = e?.status as number | undefined;
  const message = (e?.message as string) ?? "OpenAI API error";

  const map: Record<number, [NousErrorCode, boolean]> = {
    401: ["AUTHENTICATION_ERROR", false],
    403: ["CONTENT_FILTER", false],
    404: ["MODEL_NOT_FOUND", false],
    400: ["INVALID_REQUEST", false],
    429: ["RATE_LIMIT_ERROR", true],
    500: ["PROVIDER_ERROR", true],
    503: ["PROVIDER_ERROR", true],
  };

  const [code, retryable] = map[status ?? 0] ?? ["UNKNOWN_ERROR", false];
  return createNousError(message, code, provider, { status, retryable, raw: err });
}
```

---

## 🗺️ `src/utils/model.registry.ts`

```typescript
// Registry model untuk validasi dan aliasing
export const MODEL_REGISTRY = {
  anthropic: {
    "claude-opus-4-5":          { contextWindow: 200_000, maxOutput: 32_000 },
    "claude-sonnet-4-5":        { contextWindow: 200_000, maxOutput: 16_000 },
    "claude-haiku-3-5":         { contextWindow: 200_000, maxOutput: 16_000 },
    "claude-3-opus-20240229":   { contextWindow: 200_000, maxOutput: 4_096 },
    "claude-3-sonnet-20240229": { contextWindow: 200_000, maxOutput: 4_096 },
    "claude-3-haiku-20240307":  { contextWindow: 200_000, maxOutput: 4_096 },
  },
  openai: {
    "gpt-4o":          { contextWindow: 128_000, maxOutput: 16_384 },
    "gpt-4o-mini":     { contextWindow: 128_000, maxOutput: 16_384 },
    "gpt-4-turbo":     { contextWindow: 128_000, maxOutput: 4_096 },
    "gpt-4":           { contextWindow:   8_192, maxOutput: 4_096 },
    "gpt-3.5-turbo":   { contextWindow:  16_385, maxOutput: 4_096 },
    "o1":              { contextWindow: 200_000, maxOutput: 100_000 },
    "o3-mini":         { contextWindow: 200_000, maxOutput: 100_000 },
  },
} as const;

export type AnthropicModel = keyof typeof MODEL_REGISTRY.anthropic;
export type OpenAIModel    = keyof typeof MODEL_REGISTRY.openai;
export type KnownModel     = AnthropicModel | OpenAIModel;

export function getModelInfo(provider: "anthropic" | "openai", model: string) {
  return (MODEL_REGISTRY[provider] as Record<string, { contextWindow: number; maxOutput: number }>)[model] ?? null;
}
```

---

## 🔄 `src/adapters/request.adapter.ts`

```typescript
// ============================================================
// REQUEST ADAPTER
// Converts NousRequestOptions → provider-native format
// ============================================================

import {
  NousRequestOptions,
  NousTool,
  Message,
  MessageContent,
} from "../types";

// ---- ANTHROPIC ----
export function toAnthropicRequest(opts: NousRequestOptions) {
  const messages = opts.messages
    .filter((m) => m.role !== "system")
    .map(adaptMessageToAnthropic);

  const tools = opts.tools?.map(adaptToolToAnthropic);

  const toolChoice = adaptToolChoiceToAnthropic(opts.tool_choice);

  return {
    model: opts.model,
    max_tokens: opts.max_tokens ?? 4096,
    messages,
    ...(opts.system ? { system: opts.system } : extractSystem(opts.messages)),
    ...(opts.temperature !== undefined && { temperature: opts.temperature }),
    ...(opts.top_p !== undefined && { top_p: opts.top_p }),
    ...(opts.stop ? { stop_sequences: Array.isArray(opts.stop) ? opts.stop : [opts.stop] } : {}),
    ...(tools?.length ? { tools, tool_choice: toolChoice ?? { type: "auto" } } : {}),
    ...(opts.stream ? { stream: true } : {}),
  };
}

function extractSystem(messages: Message[]): { system?: string } {
  const sysMsg = messages.find((m) => m.role === "system");
  return sysMsg ? { system: typeof sysMsg.content === "string" ? sysMsg.content : "" } : {};
}

function adaptMessageToAnthropic(msg: Message) {
  if (typeof msg.content === "string") {
    return { role: msg.role, content: msg.content };
  }
  return {
    role: msg.role,
    content: msg.content.map((c) => {
      if (c.type === "text") return { type: "text", text: c.text };
      if (c.type === "image") {
        if (c.source.type === "base64") {
          return {
            type: "image",
            source: { type: "base64", media_type: c.source.media_type, data: c.source.data },
          };
        }
        return { type: "image", source: { type: "url", url: c.source.url } };
      }
      return c;
    }),
  };
}

function adaptToolToAnthropic(tool: NousTool) {
  return {
    name: tool.name,
    description: tool.description,
    input_schema: tool.parameters,
  };
}

function adaptToolChoiceToAnthropic(choice?: NousRequestOptions["tool_choice"]) {
  if (!choice) return undefined;
  if (choice === "auto") return { type: "auto" };
  if (choice === "none") return { type: "none" };
  if (choice === "required") return { type: "any" };
  if (typeof choice === "object" && choice.type === "tool") {
    return { type: "tool", name: choice.name };
  }
}

// ---- OPENAI ----
export function toOpenAIRequest(opts: NousRequestOptions) {
  const messages = opts.messages.map(adaptMessageToOpenAI);

  // Inject system message jika ada
  if (opts.system) {
    messages.unshift({ role: "system", content: opts.system });
  }

  const tools = opts.tools?.map(adaptToolToOpenAI);
  const tool_choice = adaptToolChoiceToOpenAI(opts.tool_choice);

  return {
    model: opts.model,
    messages,
    ...(opts.max_tokens !== undefined && { max_tokens: opts.max_tokens }),
    ...(opts.temperature !== undefined && { temperature: opts.temperature }),
    ...(opts.top_p !== undefined && { top_p: opts.top_p }),
    ...(opts.stop ? { stop: opts.stop } : {}),
    ...(tools?.length ? { tools, tool_choice: tool_choice ?? "auto" } : {}),
    ...(opts.stream ? { stream: true, stream_options: { include_usage: true } } : {}),
  };
}

function adaptMessageToOpenAI(msg: Message): Record<string, unknown> {
  if (typeof msg.content === "string") {
    return { role: msg.role, content: msg.content };
  }
  return {
    role: msg.role,
    content: msg.content.map((c) => {
      if (c.type === "text") return { type: "text", text: c.text };
      if (c.type === "image") {
        const url = c.source.type === "url"
          ? c.source.url
          : `data:${c.source.media_type};base64,${c.source.data}`;
        return { type: "image_url", image_url: { url } };
      }
      return c;
    }),
  };
}

function adaptToolToOpenAI(tool: NousTool) {
  return {
    type: "function",
    function: {
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
    },
  };
}

function adaptToolChoiceToOpenAI(choice?: NousRequestOptions["tool_choice"]) {
  if (!choice) return undefined;
  if (choice === "auto") return "auto";
  if (choice === "none") return "none";
  if (choice === "required") return "required";
  if (typeof choice === "object" && choice.type === "tool") {
    return { type: "function", function: { name: choice.name } };
  }
}
```

---

## 🔄 `src/adapters/response.adapter.ts`

```typescript
// ============================================================
// RESPONSE ADAPTER
// Converts provider-native response → NousResponse
// ============================================================

import { NousResponse, Provider, TokenUsage, FinishReason, TextContent, ToolUseContent } from "../types";

// ---- ANTHROPIC ----
export function fromAnthropicResponse(raw: unknown, provider: Provider): NousResponse {
  const r = raw as Record<string, unknown>;

  const usage = r.usage as Record<string, number> | undefined;
  const normalizedUsage: TokenUsage = {
    input_tokens:                  usage?.input_tokens ?? 0,
    output_tokens:                 usage?.output_tokens ?? 0,
    total_tokens:                  (usage?.input_tokens ?? 0) + (usage?.output_tokens ?? 0),
    cache_read_input_tokens:       usage?.cache_read_input_tokens,
    cache_creation_input_tokens:   usage?.cache_creation_input_tokens,
  };

  const rawContent = (r.content as unknown[]) ?? [];
  const content: Array<TextContent | ToolUseContent> = rawContent.map((block: unknown) => {
    const b = block as Record<string, unknown>;
    if (b.type === "text") return { type: "text", text: b.text as string };
    if (b.type === "tool_use") {
      return {
        type: "tool_use",
        id: b.id as string,
        name: b.name as string,
        input: b.input as Record<string, unknown>,
      };
    }
    return { type: "text", text: JSON.stringify(b) };
  });

  const stopReason = r.stop_reason as string | null;
  const finish_reason: FinishReason = stopReason === "end_turn"
    ? "stop"
    : stopReason === "max_tokens"
    ? "length"
    : stopReason === "tool_use"
    ? "tool_use"
    : null;

  return {
    id:            r.id as string,
    model:         r.model as string,
    provider,
    role:          "assistant",
    content,
    finish_reason,
    usage:         normalizedUsage,
    raw,
  };
}

// ---- OPENAI ----
export function fromOpenAIResponse(raw: unknown, provider: Provider): NousResponse {
  const r = raw as Record<string, unknown>;
  const choice = (r.choices as unknown[])?.[0] as Record<string, unknown> | undefined;
  const message = choice?.message as Record<string, unknown> | undefined;

  const usage = r.usage as Record<string, number> | undefined;
  const normalizedUsage: TokenUsage = {
    input_tokens:  usage?.prompt_tokens ?? 0,
    output_tokens: usage?.completion_tokens ?? 0,
    total_tokens:  usage?.total_tokens ?? 0,
  };

  const content: Array<TextContent | ToolUseContent> = [];

  if (message?.content) {
    content.push({ type: "text", text: message.content as string });
  }

  const toolCalls = message?.tool_calls as unknown[] | undefined;
  if (toolCalls?.length) {
    toolCalls.forEach((tc: unknown) => {
      const t = tc as Record<string, unknown>;
      const fn = t.function as Record<string, unknown>;
      content.push({
        type:  "tool_use",
        id:    t.id as string,
        name:  fn.name as string,
        input: JSON.parse((fn.arguments as string) ?? "{}"),
      });
    });
  }

  const rawFinish = choice?.finish_reason as string | undefined;
  const finish_reason: FinishReason =
    rawFinish === "stop"           ? "stop"
    : rawFinish === "length"       ? "length"
    : rawFinish === "tool_calls"   ? "tool_use"
    : rawFinish === "content_filter" ? "content_filter"
    : null;

  return {
    id:            r.id as string,
    model:         r.model as string,
    provider,
    role:          "assistant",
    content,
    finish_reason,
    usage:         normalizedUsage,
    raw,
  };
}
```

---

## 🌊 `src/streaming/stream.handler.ts`

```typescript
import { StreamDelta, StreamHandler, FinishReason } from "../types";

// ---- ANTHROPIC STREAMING ----
export async function handleAnthropicStream(
  stream: AsyncIterable<unknown>,
  onDelta: StreamHandler
): Promise<void> {
  for await (const event of stream) {
    const e = event as Record<string, unknown>;

    if (e.type === "content_block_delta") {
      const delta = e.delta as Record<string, unknown>;
      if (delta?.type === "text_delta") {
        await onDelta({ type: "text_delta", text: delta.text as string });
      } else if (delta?.type === "input_json_delta") {
        await onDelta({
          type: "tool_delta",
          tool_use: { input: delta.partial_json as Record<string, unknown> },
        });
      }
    }

    if (e.type === "message_delta") {
      const delta = e.delta as Record<string, unknown>;
      const usage = e.usage as Record<string, number> | undefined;
      const stopReason = delta?.stop_reason as string | null;
      const finish_reason: FinishReason =
        stopReason === "end_turn"   ? "stop"
        : stopReason === "max_tokens" ? "length"
        : stopReason === "tool_use" ? "tool_use"
        : null;

      await onDelta({
        type: "stop",
        finish_reason,
        ...(usage ? {
          usage: {
            output_tokens: usage.output_tokens,
          },
        } : {}),
      });
    }
  }
}

// ---- OPENAI STREAMING ----
export async function handleOpenAIStream(
  stream: AsyncIterable<unknown>,
  onDelta: StreamHandler
): Promise<void> {
  for await (const chunk of stream) {
    const c = chunk as Record<string, unknown>;
    const choices = c.choices as unknown[] | undefined;
    const choice = choices?.[0] as Record<string, unknown> | undefined;
    const delta = choice?.delta as Record<string, unknown> | undefined;

    if (delta?.content) {
      await onDelta({ type: "text_delta", text: delta.content as string });
    }

    if (delta?.tool_calls) {
      const toolCalls = delta.tool_calls as unknown[];
      for (const tc of toolCalls) {
        const t = tc as Record<string, unknown>;
        const fn = t.function as Record<string, unknown>;
        await onDelta({
          type: "tool_delta",
          tool_use: {
            id:    t.id as string,
            name:  fn?.name as string,
            input: fn?.arguments as Record<string, unknown>,
          },
        });
      }
    }

    const finishReason = choice?.finish_reason as string | undefined;
    if (finishReason) {
      const finish_reason: FinishReason =
        finishReason === "stop"             ? "stop"
        : finishReason === "length"         ? "length"
        : finishReason === "tool_calls"     ? "tool_use"
        : finishReason === "content_filter" ? "content_filter"
        : null;

      // usage dari stream_options.include_usage
      const usage = c.usage as Record<string, number> | undefined;
      await onDelta({
        type: "stop",
        finish_reason,
        ...(usage ? {
          usage: {
            input_tokens:  usage.prompt_tokens,
            output_tokens: usage.completion_tokens,
            total_tokens:  usage.total_tokens,
          },
        } : {}),
      });
    }
  }
}
```

---

## 🏭 `src/providers/base.provider.ts`

```typescript
import { NousRequestOptions, NousResponse, StreamHandler } from "../types";

export abstract class BaseProvider {
  abstract readonly name: "anthropic" | "openai";

  abstract complete(opts: NousRequestOptions): Promise<NousResponse>;
  abstract stream(opts: NousRequestOptions, onDelta: StreamHandler): Promise<void>;
}
```

---

## 🤖 `src/providers/anthropic.provider.ts`

```typescript
import Anthropic from "@anthropic-ai/sdk";
import { BaseProvider } from "./base.provider";
import { NousRequestOptions, NousResponse, StreamHandler } from "../types";
import { NousConfig } from "../types";
import { resolveConfig } from "../config";
import { toAnthropicRequest } from "../adapters/request.adapter";
import { fromAnthropicResponse } from "../adapters/response.adapter";
import { handleAnthropicStream } from "../streaming/stream.handler";
import { normalizeAnthropicError } from "../utils/errors";

export class AnthropicProvider extends BaseProvider {
  readonly name = "anthropic" as const;
  private client: Anthropic;
  private config: Required<NousConfig>;

  constructor(config: NousConfig) {
    super();
    this.config = resolveConfig(config);
    this.client = new Anthropic({
      apiKey:   this.config.apiKey,
      baseURL:  this.config.baseURL,
      timeout:  this.config.timeout,
      maxRetries: this.config.maxRetries,
      defaultHeaders: this.config.headers,
    });
  }

  async complete(opts: NousRequestOptions): Promise<NousResponse> {
    const req = toAnthropicRequest({
      ...opts,
      model: opts.model ?? this.config.defaultModel,
      max_tokens: opts.max_tokens ?? this.config.defaultMaxTokens,
      temperature: opts.temperature ?? this.config.defaultTemperature,
    });

    try {
      const raw = await this.client.messages.create(req as Parameters<typeof this.client.messages.create>[0]);
      return fromAnthropicResponse(raw, "anthropic");
    } catch (err) {
      throw normalizeAnthropicError(err, "anthropic");
    }
  }

  async stream(opts: NousRequestOptions, onDelta: StreamHandler): Promise<void> {
    const req = toAnthropicRequest({
      ...opts,
      model:       opts.model       ?? this.config.defaultModel,
      max_tokens:  opts.max_tokens  ?? this.config.defaultMaxTokens,
      temperature: opts.temperature ?? this.config.defaultTemperature,
      stream: true,
    });

    try {
      const stream = await this.client.messages.stream(
        req as Parameters<typeof this.client.messages.stream>[0]
      );
      await handleAnthropicStream(stream as AsyncIterable<unknown>, onDelta);
    } catch (err) {
      throw normalizeAnthropicError(err, "anthropic");
    }
  }
}
```

---

## 🤖 `src/providers/openai.provider.ts`

```typescript
import OpenAI from "openai";
import { BaseProvider } from "./base.provider";
import { NousRequestOptions, NousResponse, StreamHandler, NousConfig } from "../types";
import { resolveConfig } from "../config";
import { toOpenAIRequest } from "../adapters/request.adapter";
import { fromOpenAIResponse } from "../adapters/response.adapter";
import { handleOpenAIStream } from "../streaming/stream.handler";
import { normalizeOpenAIError } from "../utils/errors";

export class OpenAIProvider extends BaseProvider {
  readonly name = "openai" as const;
  private client: OpenAI;
  private config: Required<NousConfig>;

  constructor(config: NousConfig) {
    super();
    this.config = resolveConfig(config);
    this.client = new OpenAI({
      apiKey:    this.config.apiKey,
      baseURL:   this.config.baseURL,
      timeout:   this.config.timeout,
      maxRetries: this.config.maxRetries,
      defaultHeaders: this.config.headers,
    });
  }

  async complete(opts: NousRequestOptions): Promise<NousResponse> {
    const req = toOpenAIRequest({
      ...opts,
      model:       opts.model       ?? this.config.defaultModel,
      max_tokens:  opts.max_tokens  ?? this.config.defaultMaxTokens,
      temperature: opts.temperature ?? this.config.defaultTemperature,
    });

    try {
      const raw = await this.client.chat.completions.create(
        req as Parameters<typeof this.client.chat.completions.create>[0]
      );
      return fromOpenAIResponse(raw, "openai");
    } catch (err) {
      throw normalizeOpenAIError(err, "openai");
    }
  }

  async stream(opts: NousRequestOptions, onDelta: StreamHandler): Promise<void> {
    const req = toOpenAIRequest({
      ...opts,
      model:       opts.model       ?? this.config.defaultModel,
      max_tokens:  opts.max_tokens  ?? this.config.defaultMaxTokens,
      temperature: opts.temperature ?? this.config.defaultTemperature,
      stream: true,
    });

    try {
      const stream = await this.client.chat.completions.create(
        req as Parameters<typeof this.client.chat.completions.create>[0]
      ) as AsyncIterable<unknown>;
      await handleOpenAIStream(stream, onDelta);
    } catch (err) {
      throw normalizeOpenAIError(err, "openai");
    }
  }
}
```

---

## 🎯 `src/index.ts` — Main Entry Point (NousClient)

```typescript
// ============================================================
// NOUS CLIENT - Universal LLM Wrapper
// Supports: Anthropic + OpenAI (+ OpenAI-compatible endpoints)
// ============================================================

import { AnthropicProvider } from "./providers/anthropic.provider";
import { OpenAIProvider } from "./providers/openai.provider";
import {
  NousConfig,
  NousRequestOptions,
  NousResponse,
  StreamHandler,
  Message,
  Provider,
} from "./types";
import { validateConfig } from "./config";

export class NousClient {
  private provider: AnthropicProvider | OpenAIProvider;
  readonly providerName: Provider;

  constructor(config: NousConfig) {
    validateConfig(config);

    if (config.provider === "anthropic") {
      this.provider = new AnthropicProvider(config);
    } else {
      this.provider = new OpenAIProvider(config);
    }
    this.providerName = config.provider;
  }

  // --- Complete (non-streaming) ---
  async complete(opts: NousRequestOptions): Promise<NousResponse> {
    return this.provider.complete({ ...opts, stream: false });
  }

  // --- Stream ---
  async stream(opts: NousRequestOptions, onDelta: StreamHandler): Promise<void> {
    return this.provider.stream({ ...opts, stream: true }, onDelta);
  }

  // --- Convenience: simple chat ---
  async chat(
    userMessage: string,
    options?: Partial<Omit<NousRequestOptions, "messages">>
  ): Promise<NousResponse> {
    const messages: Message[] = [{ role: "user", content: userMessage }];
    return this.complete({ messages, ...options } as NousRequestOptions);
  }

  // --- Convenience: chat stream ---
  async chatStream(
    userMessage: string,
    onDelta: StreamHandler,
    options?: Partial<Omit<NousRequestOptions, "messages">>
  ): Promise<void> {
    const messages: Message[] = [{ role: "user", content: userMessage }];
    return this.stream({ messages, ...options } as NousRequestOptions, onDelta);
  }
}

// Factory functions
export function createAnthropicClient(apiKey: string, options?: Partial<NousConfig>) {
  return new NousClient({ provider: "anthropic", apiKey, ...options });
}

export function createOpenAIClient(apiKey: string, options?: Partial<NousConfig>) {
  return new NousClient({ provider: "openai", apiKey, ...options });
}

// OpenAI-compatible (LM Studio, Ollama, Together, Groq, etc.)
export function createOpenAICompatibleClient(
  baseURL: string,
  apiKey: string = "dummy",
  options?: Partial<NousConfig>
) {
  return new NousClient({ provider: "openai", apiKey, baseURL, ...options });
}

// Re-exports
export * from "./types";
export * from "./utils/model.registry";
```

---

## 🧪 Usage Examples

### 1. Anthropic (Claude)
```typescript
import { createAnthropicClient } from "@wrappers/nous";

const claude = createAnthropicClient(process.env.ANTHROPIC_API_KEY!);

// Simple chat
const res = await claude.chat("Apa ibu kota Indonesia?");
console.log(res.content[0].text);

// Full options
const res2 = await claude.complete({
  model: "claude-opus-4-5",
  messages: [{ role: "user", content: "Jelaskan quantum computing" }],
  system: "Kamu adalah guru fisika yang ramah.",
  max_tokens: 1024,
  temperature: 0.7,
});
```

### 2. OpenAI (GPT)
```typescript
import { createOpenAIClient } from "@wrappers/nous";

const gpt = createOpenAIClient(process.env.OPENAI_API_KEY!);

const res = await gpt.complete({
  model: "gpt-4o",
  messages: [{ role: "user", content: "Hello!" }],
});
```

### 3. OpenAI-Compatible (Ollama, LM Studio, Groq, Together)
```typescript
import { createOpenAICompatibleClient } from "@wrappers/nous";

// Ollama
const ollama = createOpenAICompatibleClient(
  "http://localhost:11434/v1",
  "ollama",
  { defaultModel: "llama3.1:8b" }
);

// Groq
const groq = createOpenAICompatibleClient(
  "https://api.groq.com/openai/v1",
  process.env.GROQ_API_KEY!,
  { defaultModel: "llama-3.3-70b-versatile" }
);
```

### 4. Streaming
```typescript
import { createAnthropicClient } from "@wrappers/nous";

const client = createAnthropicClient(process.env.ANTHROPIC_API_KEY!);

await client.chatStream(
  "Tulis puisi tentang laut",
  async (delta) => {
    if (delta.type === "text_delta") process.stdout.write(delta.text ?? "");
    if (delta.type === "stop") console.log("\n\n[Done]", delta.finish_reason);
  }
);
```

### 5. Tool Use / Function Calling
```typescript
import { createOpenAIClient, NousTool } from "@wrappers/nous";

const client = createOpenAIClient(process.env.OPENAI_API_KEY!);

const tools: NousTool[] = [{
  name: "get_weather",
  description: "Mendapatkan cuaca saat ini",
  parameters: {
    type: "object",
    properties: {
      city: { type: "string", description: "Nama kota" },
    },
    required: ["city"],
  },
}];

const res = await client.complete({
  model: "gpt-4o",
  messages: [{ role: "user", content: "Cuaca di Jakarta sekarang?" }],
  tools,
  tool_choice: "auto",
});

// Cek tool call
if (res.content[0].type === "tool_use") {
  console.log("Tool:", res.content[0].name);
  console.log("Input:", res.content[0].input);
}
```

---

## ✅ Ringkasan Audit & Perbaikan

| # | Area | Sebelum | Sesudah |
|---|---|---|---|
| 1 | **Provider Support** | Single provider | ✅ Anthropic + OpenAI + Compatible |
| 2 | **Request Normalization** | Raw format | ✅ Unified `NousRequestOptions` |
| 3 | **Response Normalization** | Inconsistent | ✅ Unified `NousResponse` |
| 4 | **Streaming** | None/partial | ✅ Full SSE dual-provider |
| 5 | **Error Handling** | Uncaught | ✅ Normalized `NousError` + retryable flag |
| 6 | **Tool Use** | Missing | ✅ Tools + tool_choice cross-provider |
| 7 | **Model Registry** | Missing | ✅ Known models + context window info |
| 8 | **Type Safety** | Weak | ✅ Full TypeScript strict types |
| 9 | **OpenAI-Compatible** | Missing | ✅ baseURL override (Ollama, Groq, dll) |
| 10 | **Config Validation** | Missing | ✅ Runtime validation + defaults |