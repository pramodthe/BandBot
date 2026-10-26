import { api, ApiError } from "@/api";
import type { Action } from "@/store";
import type { Message } from "@/types";

function retryable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status >= 500 || error.status === 408 || error.status === 429 || error.status < 400;
}

/** Send a human message. The same clientId makes a lost response safe to retry. */
export async function deliverMessage(
  dispatch: (action: Action) => void,
  channelId: number,
  content: string,
  attachmentIds: string[],
  clientId: string,
): Promise<void> {
  dispatch({
    type: "outgoing_upsert",
    message: {
      clientId,
      channelId,
      content,
      attachmentIds,
      createdAt: new Date().toISOString(),
      status: "pending",
      retryable: true,
    },
  });
  try {
    const message = await api<Message>("POST", `/api/channels/${channelId}/messages`, {
      content,
      attachment_ids: attachmentIds,
      client_id: clientId,
    });
    dispatch({ type: "message_settled", message });
  } catch (error) {
    dispatch({
      type: "outgoing_fail",
      channelId,
      clientId,
      error: error instanceof Error ? error.message : "Couldn't send",
      retryable: retryable(error),
    });
  }
}
