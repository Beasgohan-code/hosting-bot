import { describe, expect, it, vi } from "vitest";
import { sendTelegramMonitorAlert } from "./notifications";

const config = { botToken: "123456:secret-token", chatId: "-100123456" };
const alert = {
  state: "down" as const,
  monitorName: "Production API",
  monitorUrl: "https://status.example.org/health",
  detail: "HTTP 503",
};

const telegramResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "content-type": "application/json" },
});

describe("Telegram monitor notifications", () => {
  it("sends a transition message to the configured chat", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => telegramResponse({ ok: true }));
    const result = await sendTelegramMonitorAlert(config, alert, fetcher);

    expect(result).toEqual({ sent: true });
    expect(fetcher).toHaveBeenCalledOnce();
    const [url, init] = fetcher.mock.calls[0]!;
    expect(url).toBe("https://api.telegram.org/bot123456:secret-token/sendMessage");
    expect(init?.method).toBe("POST");
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    expect(body).toMatchObject({
      chat_id: config.chatId,
      text: expect.stringContaining("Production API"),
      disable_web_page_preview: true,
    });
    expect(body.text).toContain("HTTP 503");
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("does not attempt delivery without both credentials", async () => {
    const fetcher = vi.fn();
    const result = await sendTelegramMonitorAlert({ botToken: "", chatId: config.chatId }, alert, fetcher);
    expect(result.sent).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("reports a Telegram HTTP failure without treating it as delivered", async () => {
    const result = await sendTelegramMonitorAlert(config, alert, async () => telegramResponse({}, 502));
    expect(result).toEqual({ sent: false, error: "Telegram returned HTTP 502." });
  });

  it("reports an API-level rejection", async () => {
    const result = await sendTelegramMonitorAlert(config, alert, async () => telegramResponse({
      ok: false,
      description: "Chat not found",
    }));
    expect(result).toEqual({ sent: false, error: "Chat not found" });
  });

  it("contains network errors and never rejects the monitor loop", async () => {
    const result = await sendTelegramMonitorAlert(config, alert, async () => {
      throw new Error("network unavailable");
    });
    expect(result).toEqual({ sent: false, error: "network unavailable" });
  });
});
