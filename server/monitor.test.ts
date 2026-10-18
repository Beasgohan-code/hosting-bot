import { describe, expect, it, vi } from "vitest";
import { isPublicAddress, parseMonitorUrl, probeMonitorTarget, validateMonitorUrl } from "./monitor";

describe("keep-alive target safety", () => {
  it.each([
    "0.0.0.0",
    "10.2.3.4",
    "127.0.0.1",
    "172.20.1.1",
    "192.168.1.25",
    "169.254.169.254",
    "192.0.2.1",
    "198.51.100.3",
    "203.0.113.8",
    "224.0.0.1",
    "::",
    "::1",
    "fc00::1",
    "fe80::1",
    "2001:20::1",
    "2001:db8::1",
    "2002::1",
    "3ffe::1",
    "3fff::1",
    "::ffff:10.0.0.1",
  ])("rejects non-public address %s", (address) => {
    expect(isPublicAddress(address)).toBe(false);
  });

  it.each(["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2001:4860:4860::8888", "::ffff:8.8.8.8"]) (
    "allows public address %s",
    (address) => expect(isPublicAddress(address)).toBe(true),
  );

  it("accepts a public HTTPS health URL", () => {
    expect(parseMonitorUrl("https://status.example.org/health").pathname).toBe("/health");
  });

  it.each([
    "http://localhost/health",
    "http://127.0.0.1/health",
    "http://10.0.0.2/health",
    "http://service.internal/health",
    "http://user:pass@example.org/health",
    "https://example.org/health?token=secret",
    "file:///etc/passwd",
  ])("rejects unsafe URL %s", (url) => {
    expect(() => parseMonitorUrl(url)).toThrow();
  });

  it("rejects a DNS name that resolves to any private address", async () => {
    const requestPinned = vi.fn(async () => 200);
    await expect(validateMonitorUrl("https://status.example.org/health", async () => [
      { address: "93.184.216.34", family: 4 },
      { address: "10.0.0.8", family: 4 },
    ])).rejects.toThrow(/only to public IP/);
    expect(requestPinned).not.toHaveBeenCalled();
  });

  it("pins a probe to the already-validated address instead of resolving again", async () => {
    const requestPinned = vi.fn(async () => 204);
    const result = await probeMonitorTarget("https://status.example.org/health", {
      resolveHost: async (hostname) => {
        expect(hostname).toBe("status.example.org");
        return [{ address: "93.184.216.34", family: 4 }];
      },
      requestPinned,
    });

    expect(result).toMatchObject({ ok: true, statusCode: 204, error: null });
    expect(requestPinned).toHaveBeenCalledWith(
      expect.objectContaining({ hostname: "status.example.org", pathname: "/health" }),
      { address: "93.184.216.34", family: 4 },
      10_000,
    );
  });

  it.each([301, 302, 304, 400, 503])("does not treat HTTP %s as a healthy check", async (statusCode) => {
    const result = await probeMonitorTarget("https://status.example.org/health", {
      resolveHost: async () => [{ address: "93.184.216.34", family: 4 }],
      requestPinned: async () => statusCode,
    });
    expect(result.ok).toBe(false);
    expect(result.statusCode).toBe(statusCode);
  });

  it("reports a redirect explicitly without following it", async () => {
    const result = await probeMonitorTarget("https://status.example.org/health", {
      resolveHost: async () => [{ address: "93.184.216.34", family: 4 }],
      requestPinned: async () => 302,
    });
    expect(result.error).toContain("Redirect response is not followed");
  });
});
