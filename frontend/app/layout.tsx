import type { Metadata, Viewport } from "next";
import "./globals.css";
// Fonts are bundled (OFL-1.1, @fontsource): no runtime requests to Google, and they work offline.
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/600.css";
import "@fontsource/jetbrains-mono/700.css";
import "@fontsource/orbitron/500.css";
import "@fontsource/orbitron/700.css";

export const metadata: Metadata = { title: "RR Trading Engine — AI Command Center", description: "Institutional AI trading command center" };
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#020304" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head />
      <body>{children}</body>
    </html>
  );
}
