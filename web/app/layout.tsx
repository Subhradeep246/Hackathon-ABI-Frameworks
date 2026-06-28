import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Wound IQ",
  description: "Medicare Part B wound care billing eligibility",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
