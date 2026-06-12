type WsHandler = (data: { channel: string; data: Record<string, unknown> }) => void;
type ReconnectHandler = () => void;

export class WsClient {
  private ws: WebSocket | null = null;
  private channels: string[] = [];
  private handlers: WsHandler[] = [];
  private reconnectHandlers: ReconnectHandler[] = [];
  private retryDelay = 1000;
  private maxDelay = 30000;
  private url: string;
  private destroyed = false;
  private hasConnectedOnce = false;

  constructor(url: string) {
    this.url = url;
  }

  connect() {
    if (this.destroyed) return;
    // /ws 已加 token 认证（worker 模式必须）；浏览器设不了 header，走查询参数
    const token = localStorage.getItem('cc_token') || '';
    const url = token ? `${this.url}${this.url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : this.url;
    this.ws = new WebSocket(url);
    this.ws.onopen = () => {
      this.retryDelay = 1000;
      if (this.channels.length > 0) {
        this.ws?.send(JSON.stringify({ action: 'subscribe', channels: this.channels }));
      }
      // Notify reconnect handlers (skip the very first connect)
      if (this.hasConnectedOnce) {
        this.reconnectHandlers.forEach((h) => h());
      }
      this.hasConnectedOnce = true;
    };
    this.ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.channel) {
          this.handlers.forEach((h) => h(msg));
        }
      } catch { /* ignore */ }
    };
    this.ws.onclose = () => {
      if (this.destroyed) return;
      setTimeout(() => this.connect(), this.retryDelay);
      this.retryDelay = Math.min(this.retryDelay * 2, this.maxDelay);
    };
  }

  subscribe(channels: string[]) {
    this.channels = [...new Set([...this.channels, ...channels])];
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ action: 'subscribe', channels }));
    }
  }

  onMessage(handler: WsHandler) {
    this.handlers.push(handler);
    return () => {
      this.handlers = this.handlers.filter((h) => h !== handler);
    };
  }

  onReconnect(handler: ReconnectHandler) {
    this.reconnectHandlers.push(handler);
    return () => {
      this.reconnectHandlers = this.reconnectHandlers.filter((h) => h !== handler);
    };
  }

  close() {
    this.destroyed = true;
    this.ws?.close();
    this.ws = null;
  }
}
