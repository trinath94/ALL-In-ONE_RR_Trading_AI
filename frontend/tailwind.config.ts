import type { Config } from "tailwindcss";
export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        void: "#020304", panel: "#0a0d10", panel2: "#0f1418", line: "#1a2329",
        neon: "#39ff88", cyan: "#22e4ff", cyber: "#2f7bff", violet: "#b45cff", bear: "#ff3d5a", warn: "#ffb020",
        dim: "#6b7f8a", text: "#d7e3ea",
      },
      fontFamily: {
        mono: ["'JetBrains Mono'", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
        display: ["Orbitron", "'JetBrains Mono'", "ui-monospace", "monospace"],
      },
      boxShadow: { neon: "0 0 0 1px rgba(57,255,136,.25), 0 0 18px rgba(57,255,136,.08)",
                   cyan: "0 0 0 1px rgba(34,228,255,.25), 0 0 18px rgba(34,228,255,.08)",
                   bear: "0 0 0 1px rgba(255,61,90,.35), 0 0 18px rgba(255,61,90,.12)" },
    },
  },
  plugins: [],
} satisfies Config;
