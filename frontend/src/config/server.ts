const STORAGE_KEY = 'cc_server_url';

export function isCapacitor(): boolean {
  return !!(window as unknown as Record<string, unknown>).Capacitor;
}

/** Returns true if a remote server URL has been configured (regardless of platform) */
export function hasRemoteServer(): boolean {
  return getServerUrl() !== '';
}

export function needsServerConfig(): boolean {
  // In Capacitor, always need server config if not set
  if (isCapacitor()) return !getServerUrl();
  // On web, if user manually set a server URL, use it
  return false;
}

export function getServerUrl(): string {
  return localStorage.getItem(STORAGE_KEY) || '';
}

export function setServerUrl(url: string) {
  // Normalize: remove trailing slash
  const normalized = url.replace(/\/+$/, '');
  localStorage.setItem(STORAGE_KEY, normalized);
}

export function clearServerUrl() {
  localStorage.removeItem(STORAGE_KEY);
}

export function getApiBase(): string {
  if (!isCapacitor()) return '';
  return getServerUrl();
}

/** 后端返回的资源相对路径（如 /api/uploads/x.png）在 Capacitor App 里
 *  会相对 capacitor://localhost 解析而 404 —— 统一经此拼上远程服务器地址。
 *  Web 端 getApiBase() 为空串，原样返回。 */
export function resolveAssetUrl(url: string): string {
  return url.startsWith('/') ? `${getApiBase()}${url}` : url;
}

export function getWsUrl(): string {
  if (!isCapacitor()) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}/ws`;
  }
  const server = getServerUrl();
  if (!server) return '';
  const wsProtocol = server.startsWith('https') ? 'wss:' : 'ws:';
  const host = server.replace(/^https?:\/\//, '');
  return `${wsProtocol}//${host}/ws`;
}
