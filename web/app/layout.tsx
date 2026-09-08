import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";

// Mono carries the interface, sans carries prose. Nearly every element on this
// page is a measured value, so mono is the content's native voice rather than a
// terminal costume.
const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-mono",
  display: "swap",
});

const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-sans",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Agent console — Lyzr FDE take-home",
  description:
    "Eleven agent projects, each defined by the failure it survives. Trigger each failure and watch the guard hold.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${mono.variable} ${sans.variable}`}>{children}</body>
    </html>
  );
}
