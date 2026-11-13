import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "RR Trading Engine — AI Command Center", description: "Institutional AI trading command center" };
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#020304" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Orbitron:wght@500;700&display=swap" rel="stylesheet" />
      </head>
      <body>{children}</body>
    </html>
  );
}
