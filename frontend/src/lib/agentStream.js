/**
 * agentStream.js
 * ──────────────
 * Consumes the backend's Server-Sent-Events chat stream and turns the raw
 * events into a structured, renderable "turn" — mirroring the backend
 * _EventCollector so live streaming and stored history render identically.
 *
 * Block shapes (match backend app/agents/shared/streaming.py):
 *   { type: 'text',           text, agent, is_subagent }
 *   { type: 'tool_call',      tool, args, result?, truncated?, tool_call_id, is_subagent }
 *   { type: 'shell',          command, is_subagent }
 *   { type: 'transfer',       from, to, is_subagent }
 *   { type: 'middleware',     kind, detail, is_subagent }   // kind 'todos' -> detail = todo list
 *   { type: 'summarizing' }
 *   { type: 'model_fallback' }
 */

/** Parse a single SSE record ("event: x\ndata: {...}"). */
export function parseSSE(raw) {
  const lines = raw.split('\n');
  let event = 'message';
  const dataLines = [];
  for (const line of lines) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^\s/, ''));
  }
  if (!dataLines.length) return null;
  let data;
  try {
    data = JSON.parse(dataLines.join('\n'));
  } catch {
    data = { raw: dataLines.join('\n') };
  }
  return { event, data };
}

/**
 * POST to /chat and consume the SSE stream.
 * Calls onEvent({event, data}) for every parsed event.
 * Returns the X-History-Id header (string | null).
 */
export async function streamChat(apiFetch, body, { onEvent, onHistoryId, signal } = {}) {
  const res = await apiFetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok || !res.body) {
    let msg = `Request failed (${res.status})`;
    try {
      const j = await res.json();
      msg = j.detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }

  const historyId = res.headers.get('X-History-Id');
  // The history id is available from the response header immediately (before
  // the body streams), so notify the caller right away — this lets the UI bind
  // the stream to the right conversation even for a brand-new chat.
  if (historyId && onHistoryId) onHistoryId(historyId);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const evt = parseSSE(raw);
      if (evt) onEvent(evt);
    }
  }
  if (buffer.trim()) {
    const evt = parseSSE(buffer);
    if (evt) onEvent(evt);
  }

  return historyId;
}

/** Fold one SSE event into the streaming turn, returning a new turn object. */
export function reduceEvent(turn, { event, data }) {
  const blocks = turn.blocks.slice();
  const last = blocks[blocks.length - 1];

  switch (event) {
    case 'text_delta': {
      const isSub = !!data.is_subagent;
      if (last && last.type === 'text' && last.is_subagent === isSub && last.agent === data.agent) {
        blocks[blocks.length - 1] = { ...last, text: last.text + (data.delta || '') };
      } else {
        blocks.push({ type: 'text', text: data.delta || '', agent: data.agent, is_subagent: isSub });
      }
      return { ...turn, blocks };
    }
    case 'tool_call_start':
      blocks.push({
        type: 'tool_call',
        tool: data.tool,
        args: data.args,
        tool_call_id: data.tool_call_id,
        agent: data.agent,
        is_subagent: !!data.is_subagent,
      });
      return { ...turn, blocks };
    case 'tool_result': {
      for (let i = blocks.length - 1; i >= 0; i--) {
        if (
          blocks[i].type === 'tool_call' &&
          blocks[i].tool_call_id === data.tool_call_id &&
          blocks[i].result === undefined
        ) {
          blocks[i] = { ...blocks[i], result: data.content, truncated: data.truncated };
          return { ...turn, blocks };
        }
      }
      blocks.push({
        type: 'tool_call',
        tool: data.tool,
        tool_call_id: data.tool_call_id,
        result: data.content,
        truncated: data.truncated,
        agent: data.agent,
        is_subagent: !!data.is_subagent,
      });
      return { ...turn, blocks };
    }
    case 'shell_command':
      blocks.push({ type: 'shell', command: data.command, agent: data.agent, is_subagent: !!data.is_subagent });
      return { ...turn, blocks };
    case 'transfer':
      blocks.push({ type: 'transfer', from: data.from, to: data.to, is_subagent: !!data.is_subagent });
      return { ...turn, blocks };
    case 'middleware':
      blocks.push({ type: 'middleware', kind: data.kind, detail: data.detail, agent: data.agent, is_subagent: !!data.is_subagent });
      return { ...turn, blocks };
    case 'context_summarizing':
      blocks.push({ type: 'summarizing', agent: data.agent });
      return { ...turn, blocks };
    case 'model_fallback':
      blocks.push({ type: 'model_fallback', agent: data.agent });
      return { ...turn, blocks };
    case 'agent_message':
      if (!data.is_subagent) {
        return {
          ...turn,
          finalText: data.content || turn.finalText,
          meta: { model: data.model, tokens: data.tokens, latency_ms: data.latency_ms },
        };
      }
      return turn;
    case 'interrupt':
      // HITL pause — surface the pending tool call(s) for approval.
      return { ...turn, interrupt: data };
    case 'files':
      // Files generated this turn — show preview/download links.
      return { ...turn, files: Array.isArray(data.files) ? data.files : [] };
    case 'token_usage':
      // Token usage summary for the completed turn.
      return {
        ...turn,
        tokens: data.tokens,
        model: data.model,
      };
    case 'error':
      return { ...turn, error: data.message || 'Stream error' };
    default:
      return turn;
  }
}

/**
 * Split a live streaming turn into (timeline, final answer text).
 * The last main-agent text run is the answer; everything else is the
 * collapsible timeline. Mirrors backend _EventCollector.split_final().
 */
export function splitFinal(blocks, finalText) {
  const arr = blocks.slice();
  let final = finalText || '';
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i].type === 'text' && !arr[i].is_subagent) {
      if (!final) final = arr[i].text;
      arr.splice(i, 1);
      break;
    }
  }
  return { timeline: arr, final };
}
