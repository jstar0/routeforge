// Top-level await: main.tsx is evaluated only after the shims are in place.
// The env check must stay inline so a normal build drops the chunk and the recording.
if (import.meta.env.VITE_REPLAY === "1") await (await import("./install")).installReplay();
