const API = "http://127.0.0.1:47632";

export type BackendState = {
  service: string;
  last_event: string;
  last_event_at: number;
  enabled: boolean;
  recording: boolean;
  engine: string;
  last_text: string;
  raw_text: string;
  last_error: string;
  audio_mode: string;
  mic_guarded: boolean;
  exclusive: boolean;
  floating_bubble: boolean;
  input_device_index: number | null;
  language: string;
};

export type BackendPayload = {
  ok?: boolean;
  state?: BackendState;
};

export type BackendInfo = {
  port: number;
  backend_path: string;
  backend_token: string;
  app_data_dir: string;
  version: string;
  running: boolean;
};

export type Device = {
  index: number;
  name: string;
  default: boolean;
};

export async function backendApi<T>(path: string, init?: RequestInit, token?: string): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("content-type", "application/json");
  if (token) headers.set("x-vernest-token", token);

  const res = await fetch(`${API}${path}`, {
    ...init,
    headers,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<T>;
}
