import { expect, test } from "@playwright/test";

test("desktop binary WebSockets pass through the API dev proxy", async ({ page }) => {
  await page.goto("/");
  const frames = await page.evaluate(() => new Promise<number[][]>((resolve, reject) => {
    const url = new URL("/api/e2e/desktop-transport", window.location.origin);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url, "binary");
    socket.binaryType = "arraybuffer";
    const received: number[][] = [];
    const timeout = window.setTimeout(() => { socket.close(); reject(new Error("Desktop proxy did not deliver binary frames")); }, 5000);
    socket.onerror = () => { window.clearTimeout(timeout); reject(new Error("Desktop proxy connection failed")); };
    socket.onmessage = (event) => {
      received.push(Array.from(new Uint8Array(event.data as ArrayBuffer)));
      if (received.length === 1) socket.send(new Uint8Array([1, 2, 3, 4]));
      else { window.clearTimeout(timeout); socket.close(); resolve(received); }
    };
  }));
  expect(new TextDecoder().decode(new Uint8Array(frames[0]))).toBe("RFB 003.008\n");
  expect(frames[1]).toEqual([1, 2, 3, 4]);
});
