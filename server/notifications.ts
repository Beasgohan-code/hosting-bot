export type TelegramAlertState = "down" | "recovered";

export type TelegramAlert = {
  state: TelegramAlertState;
  monitorName: string;
  monitorUrl: string;
  detail: string;
};

export type TelegramAlertConfig = { botToken: string; chatId: string };
export type TelegramAlertResult = { sent: boolean; error?: string };

type TelegramResponse = { ok?: boolean; description?: string };
type FetchLike = typeof fetch;

export async function sendTelegramMonitorAlert(
  config: TelegramAlertConfig,
  alert: TelegramAlert,
  fetcher: FetchLike = fetch,
): Promise<TelegramAlertResult> {
  if (!config.botToken.trim() || !config.chatId.trim()) {
    return { sent: false, error: "Telegram credentials are not configured." };
  }

  try {
    const response = await fetcher(`https://api.telegram.org/bot${config.botToken}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: config.chatId,
        text: `${alert.state === "down" ? "🔴" : "🟢"} Keep-alive target ${alert.state}: ${alert.monitorName}\n${alert.monitorUrl}\n${alert.detail}`,
        disable_web_page_preview: true,
      }),
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) return { sent: false, error: `Telegram returned HTTP ${response.status}.` };

    const result = await response.json() as TelegramResponse;
    if (!result.ok) return { sent: false, error: result.description?.slice(0, 160) || "Telegram rejected the notification." };
    return { sent: true };
  } catch (error) {
    return { sent: false, error: error instanceof Error ? error.message.slice(0, 160) : "Telegram delivery failed." };
  }
}
