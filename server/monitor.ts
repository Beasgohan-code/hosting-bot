import { lookup } from "node:dns/promises";
import { isIP } from "node:net";
import { request as httpRequest, type IncomingMessage, type RequestOptions as HttpRequestOptions } from "node:http";
import { request as httpsRequest, type RequestOptions as HttpsRequestOptions } from "node:https";
import { performance } from "node:perf_hooks";

export const DEFAULT_MONITOR_INTERVAL_MINUTES = 5;
export const MIN_MONITOR_INTERVAL_MINUTES = 1;
export const MAX_MONITOR_INTERVAL_MINUTES = 60;
export const MONITOR_REQUEST_TIMEOUT_MS = 10_000;
export const MONITOR_DNS_TIMEOUT_MS = 5_000;

export type ProbeResult = {
  ok: boolean;
  statusCode: number | null;
  latencyMs: number;
  error: string | null;
};

export type ResolvedAddress = { address: string; family: 4 | 6 };
export type MonitorResolver = (hostname: string) => Promise<ResolvedAddress[]>;
export type MonitorRequester = (url: URL, address: ResolvedAddress, timeoutMs: number) => Promise<number>;
export type ProbeDependencies = { resolveHost?: MonitorResolver; requestPinned?: MonitorRequester; timeoutMs?: number };

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
  ["192.31.196.0", 24],
  ["192.52.193.0", 24],
  ["192.88.99.0", 24],
  ["192.168.0.0", 16],
  ["192.175.48.0", 24],
  ["198.18.0.0", 15],
  ["198.51.100.0", 24],
  ["203.0.113.0", 24],
  ["224.0.0.0", 4],
  ["240.0.0.0", 4],
];

const blockedIpv6Ranges: Array<[bigint, number]> = [
  [ipv6ToBigInt("2001::"), 23],       // IETF protocol assignments, including ORCHID
  [ipv6ToBigInt("2001:db8::"), 32],   // Documentation
  [ipv6ToBigInt("2002::"), 16],       // Deprecated 6to4 transition space
  [ipv6ToBigInt("3ffe::"), 16],        // Deprecated 6bone space
  [ipv6ToBigInt("3fff::"), 20],        // Documentation
];

function ipv4ToNumber(address: string): number {
  return address.split(".").reduce((value, octet) => ((value << 8) | Number(octet)) >>> 0, 0);
}

function isInIpv4Range(address: string, network: string, prefix: number): boolean {
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  return (ipv4ToNumber(address) & mask) === (ipv4ToNumber(network) & mask);
}

function ipv6ToBigInt(address: string): bigint {
  let normalized = address.toLowerCase().split("%")[0];
  if (normalized.includes(".")) {
    const lastColon = normalized.lastIndexOf(":");
    const ipv4 = ipv4ToNumber(normalized.slice(lastColon + 1));
    normalized = `${normalized.slice(0, lastColon + 1)}${(ipv4 >>> 16).toString(16)}:${(ipv4 & 0xffff).toString(16)}`;
  }

  const [left = "", right = ""] = normalized.split("::");
  const leftGroups = left ? left.split(":") : [];
  const rightGroups = right ? right.split(":") : [];
  const missingGroups = 8 - leftGroups.length - rightGroups.length;
  const groups = normalized.includes("::")
    ? [...leftGroups, ...Array(Math.max(0, missingGroups)).fill("0"), ...rightGroups]
    : leftGroups;
  return groups.reduce((value, group) => (value << 16n) | BigInt(Number.parseInt(group || "0", 16)), 0n);
}

function isInIpv6Range(address: bigint, network: bigint, prefix: number): boolean {
  const shift = BigInt(128 - prefix);
  return (address >> shift) === (network >> shift);
}

export function isPublicAddress(address: string): boolean {
  const version = isIP(address);
  if (version === 4) {
    return !blockedIpv4Ranges.some(([network, prefix]) => isInIpv4Range(address, network, prefix));
  }
  if (version !== 6) return false;

  const value = ipv6ToBigInt(address);
  const mappedIpv4Prefix = value >> 32n;
  if (mappedIpv4Prefix === 0xffffn) {
    const ipv4 = Number(value & 0xffffffffn);
    const mappedAddress = [24, 16, 8, 0].map((shift) => (ipv4 >>> shift) & 0xff).join(".");
    return isPublicAddress(mappedAddress);
  }

  // Only global-unicast IPv6 space (2000::/3) is eligible for monitoring.
  if ((value >> 125n) !== 1n) return false;
  return !blockedIpv6Ranges.some(([network, prefix]) => isInIpv6Range(value, network, prefix));
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

async function systemResolveHost(hostname: string): Promise<ResolvedAddress[]> {
  const records = await lookup(hostname, { all: true, verbatim: true });
  return records.map((record) => ({ address: record.address, family: record.family as 4 | 6 }));
}

async function withDnsTimeout<T>(operation: Promise<T>): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<T>((_resolve, reject) => {
        timer = setTimeout(() => reject(new InvalidMonitorTargetError("The target hostname lookup timed out.")), MONITOR_DNS_TIMEOUT_MS);
        timer.unref();
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function resolvePublicAddresses(hostname: string, resolver: MonitorResolver): Promise<ResolvedAddress[]> {
  let addresses: ResolvedAddress[];
  const family = isIP(hostname);
  if (family) {
    addresses = [{ address: hostname, family: family as 4 | 6 }];
  } else {
    try {
      addresses = await withDnsTimeout(resolver(hostname));
    } catch (error) {
      if (error instanceof InvalidMonitorTargetError) throw error;
      throw new InvalidMonitorTargetError("The target hostname could not be resolved.");
    }
  }

  const uniqueAddresses = [...new Map(addresses.map((entry) => [entry.address, entry])).values()];
  if (!uniqueAddresses.length || uniqueAddresses.some(({ address }) => !isPublicAddress(address))) {
    throw new InvalidMonitorTargetError("The target must resolve only to public IP addresses.");
  }
  return uniqueAddresses;
}

export async function validateMonitorUrl(raw: string, resolver: MonitorResolver = systemResolveHost): Promise<URL> {
  const url = parseMonitorUrl(raw);
  const hostname = url.hostname.replace(/^\[|\]$/g, "");
  await resolvePublicAddresses(hostname, resolver);
  return url;
}

function requestPinnedAddress(url: URL, address: ResolvedAddress, timeoutMs: number): Promise<number> {
  return new Promise((resolve, reject) => {
    const originalHostname = url.hostname.replace(/^\[|\]$/g, "");
    const secure = url.protocol === "https:";
    const options = {
      protocol: url.protocol,
      hostname: address.address,
      family: address.family,
      port: Number(url.port || (secure ? 443 : 80)),
      method: "GET",
      path: `${url.pathname || "/"}${url.search}`,
      headers: {
        host: url.host,
        "user-agent": "HostingBot-KeepAlive/1.0",
        accept: "*/*",
        connection: "close",
      },
      timeout: timeoutMs,
      ...(secure && !isIP(originalHostname) ? { servername: originalHostname } : {}),
    };

    const onResponse = (response: IncomingMessage) => {
      const statusCode = response.statusCode ?? 0;
      response.destroy();
      resolve(statusCode);
    };
    const request = secure
      ? httpsRequest(options as HttpsRequestOptions, onResponse)
      : httpRequest(options as HttpRequestOptions, onResponse);
    request.setTimeout(timeoutMs, () => request.destroy(new Error("Monitor request timed out.")));
    request.on("error", reject);
    request.end();
  });
}

export async function probeMonitorTarget(raw: string, dependencies: ProbeDependencies = {}): Promise<ProbeResult> {
  const startedAt = performance.now();
  const timeoutMs = dependencies.timeoutMs ?? MONITOR_REQUEST_TIMEOUT_MS;
  try {
    const url = parseMonitorUrl(raw);
    const hostname = url.hostname.replace(/^\[|\]$/g, "");
    const addresses = await resolvePublicAddresses(hostname, dependencies.resolveHost ?? systemResolveHost);
    // Pin the connection to an address already checked above. Resolving the
    // hostname again during the request would allow DNS-rebinding SSRF.
    const statusCode = await (dependencies.requestPinned ?? requestPinnedAddress)(url, addresses[0], timeoutMs);
    const ok = statusCode >= 200 && statusCode < 300;
    const error = ok
      ? null
      : statusCode >= 300 && statusCode < 400
        ? `Redirect response is not followed (HTTP ${statusCode}).`
        : `HTTP ${statusCode}`;
    return { ok, statusCode, latencyMs: Math.round(performance.now() - startedAt), error };
  } catch (error) {
    const message = error instanceof Error ? error.message : "Monitor request failed.";
    return { ok: false, statusCode: null, latencyMs: Math.round(performance.now() - startedAt), error: message.slice(0, 180) };
  }
}
