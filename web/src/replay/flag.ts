// Static replay mode (GitHub Pages): enabled only when built with VITE_REPLAY=1.
export const REPLAY = import.meta.env.VITE_REPLAY === "1";
export const REPLAY_DIRTY_SOLVE = "回放版只能求解已录制的原始场景，不能求解改动后的草稿。请重新打开一个已保存版本再求解。";
