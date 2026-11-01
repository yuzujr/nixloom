import { isIP } from 'node:net';

const loopback = address => address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1';

// The socket address is authenticated by Tailscale, unlike browser-supplied headers.
export function allowsManagedAccess(request, authority) {
    if (process.env.NIXLOOM_DSH_LOCAL_ACCESS !== '1' || !authority) return false;
    let url;
    try { url = new URL(`http://${authority}`); } catch { return false; }
    const address = request.socket?.remoteAddress?.replace(/^::ffff:/, '');
    if (loopback(address) && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) return true;
    const hosts = JSON.parse(process.env.NIXLOOM_DSH_TAILNET_HOSTS || '[]');
    const peers = JSON.parse(process.env.NIXLOOM_DSH_TAILNET_PEERS || '[]');
    return isIP(address || '') !== 0 && hosts.includes(url.host) && (loopback(address) || peers.includes(address));
}
