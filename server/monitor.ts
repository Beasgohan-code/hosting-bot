import { lookup } from "node:dns/promises";
import { isIP } from "node:net";
import { performance } from "node:perf_hooks";

export const DEFAULT_MONITOR_INTERVAL_MINUTES = 5;
export const MIN_MONITOR_INTERVAL_MINUTES = 1;
export const MAX_MONITOR_INTERVAL_MINUTES = 60;
export const MONITOR_REQUEST_TIMEOUT_MS = 10_000;

export type ProbeResult = {
  ok: boolean;
  statusCode: number | null;
  latencyMs: number;
  error: string | null;
};

export class InvalidMonitorTargetError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvalidMonitorTargetError";
  }
}

const blockedIpv4Ranges: Array<[string, number]> = [
  ["0.0.0.0", 8],
  ["10.0.0.0", 8],
  ["100.64.0.0", 10],
  ["127.0.0.0", 8],
  ["169.254.0.0", 16],
  ["172.16.0.0", 12],
  ["192.0.0.0", 24],
  ["192.0.2.0", 24],
  ["192.88.99.0", 24],
  ["192.168.0.0", 16],
  ["198.18.0.0", 15],
  ["198.51.100.0", 24],
  ["203.0.113.0", 24],
  ["224.0.0.0", 4],
  ["240.0.0.0", 4],
];

function ipv4ToNumber(address: string): number {
  return address.split(".").reduce((value, octet) => ((value << 8) | Number(octet)) >>> 0, 0);
}

function isInIpv4Range(address: string, network: string, prefix: number): boolean {
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  return (ipv4ToNumber(address) & mask) === (ipv4ToNumber(network) & mask);
}

export function isPublicAddress(address: string): boolean {
  const version = isIP(address);
  if (version === 4) {
    return !blockedIpv4Ranges.some(([network, prefix]) => isInIpv4Range(address, network, prefix));
  }
  if (version !== 6) return false;

  const normalized = address.toLowerCase().split("%")[0];
  const mappedIpv4 = normalized.match(/::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/);
  if (mappedIpv4) return isPublicAddress(mappedIpv4[1]);

  // Only global-unicast IPv6 space (2000::/3) is routable for a monitor.
  const firstSegment = Number.parseInt(normalized.split(":")[0] || "0", 16);
  if ((firstSegment & 0xe000) !== 0x2000) return false;
  // Exclude documentation, benchmarking, transition, and special-purpose ranges.
  if (/^2001:(?:0|0000|2|0002|10|0010|db8|0db8):/i.test(normalized)) return false;
  if (normalized.startsWith("2002:")) return false;
  return true;
}

export function parseMonitorUrl(raw: string): URL {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new InvalidMonitorTargetError("Enter a valid HTTP or HTTPS URL.");
  }

  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new InvalidMonitorTargetError("Only HTTP and HTTPS targets are supported.");
  }
  if (url.username || url.password) {
    throw new InvalidMonitorTargetError("URLs with embedded credentials are not allowed.");
  }
  if (url.search || url.hash) {
    throw new InvalidMonitorTargetError("Query strings and URL fragments are not supported for safety.");
  }

  const hostname = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (!hostname || hostname === "localhost" || hostname.endsWith(".localhost") || hostname.endsWith(".local") || hostname.endsWith(".internal") || hostname.endsWith(".test")) {
    throw new InvalidMonitorTargetError("Private and local network targets are not allowed.");
  }
  if (isIP(hostname) && !isPublicAddress(hostname)) {
    throw new InvalidMonitorTargetError("Private and reserved IP addresses are not allowed.");
  }

  return url;
}

export async function validateMonitorUrl(raw: string): Promise<URL> {
  const url = parseMonitorUrl(raw);
  const hostname = url.hostname.replace(/^\[|\]$/g, "");
  let addresses: string[];
  if (isIP(hostname)) {
    addresses = [hostname];
  } else {
    try {
      addresses = (await lookup(hostname, { all: true, verbatim: true })).map((record) => record.address);
    } catch {
      throw new InvalidMonitorTargetError("The target hostname could not be resolved.");
    }
  }
  if (!addresses.length || addresses.some((address) => !isPublicAddress(address))) {
    throw new InvalidMonitorTargetError("The target must resolve only to public IP addresses.");
  }
  return url;
}

export async function probeMonitorTarget(raw: string): Promise<ProbeResult> {
  const startedAt = performance.now();
  try {
    const url = await validateMonitorUrl(raw);
    const response = await fetch(url, {
      method: "GET",
      redirect: "manual",
      signal: AbortSignal.timeout(MONITOR_REQUEST_TIMEOUT_MS),
      headers: { "user-agent": "HostingBot-KeepAlive/1.0" },
    });
    const latencyMs = Math.round(performance.now() - startedAt);
    return {
      ok: response.status >= 200 && response.status < 400,
      statusCode: response.status,
      latencyMs,
      error: response.status >= 400 ? `HTTP ${response.status}` : null,
    };
  } catch (error) {
    const latencyMs = Math.round(performance.now() - startedAt);
    const message = error instanceof Error ? error.message : "Monitor request failed.";
    return { ok: false, statusCode: null, latencyMs, error: message.slice(0, 180) };
  }
}
