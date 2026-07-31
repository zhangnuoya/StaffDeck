export enum AgentRuntime {
  Native = 'native',
  Codex = 'codex',
}

export function parseAgentRuntime(value: unknown): AgentRuntime {
  return value === AgentRuntime.Codex ? AgentRuntime.Codex : AgentRuntime.Native;
}
