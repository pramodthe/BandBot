// noVNC 1.7 exports its ES module at the package root; the published types use the older path.
declare module "@novnc/novnc" {
  export { default } from "@novnc/novnc/lib/rfb";
}
