import { describe, expect, it } from "vitest";
import { isPublicAddress, parseMonitorUrl } from "./monitor";

describe("keep-alive target safety", () => {
  it.each([
    "127.0.0.1",
    "10.2.3.4",
    "172.20.1.1",
    "192.168.1.25",
    "169.254.169.254",
    "::1",
    "fc00::1",
    "fe80::1",
  ])("rejects non-public address %s", (address) => {
    expect(isPublicAddress(address)).toBe(false);
  });

  it.each(["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"]) ("allows public address %s", (address) => {
    expect(isPublicAddress(address)).toBe(true);
  });

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
});
