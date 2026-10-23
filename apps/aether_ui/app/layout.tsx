import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import Link from "next/link";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Aether Dashboard",
  description: "Agent-Native Version Control System",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        <div className="flex h-screen">
          {/* Sidebar */}
          <aside className="w-64 bg-card border-r border-border flex flex-col">
            <div className="p-4 border-b border-border">
              <h1 className="text-xl font-bold bg-gradient-to-r from-blue-400 to-purple-500 bg-clip-text text-transparent">
                ⚡ Aether
              </h1>
              <p className="text-xs text-muted-foreground mt-1">Agent-Native VCS</p>
            </div>
            <nav className="flex-1 p-4 space-y-2">
              <Link
                href="/"
                className="flex items-center gap-2 px-3 py-2 rounded-md hover:bg-accent transition-colors"
              >
                <span>📊</span> Dashboard
              </Link>
              <Link
                href="/repos"
                className="flex items-center gap-2 px-3 py-2 rounded-md hover:bg-accent transition-colors"
              >
                <span>📁</span> Repositories
              </Link>
              <Link
                href="/proposals"
                className="flex items-center gap-2 px-3 py-2 rounded-md hover:bg-accent transition-colors"
              >
                <span>📝</span> Proposals
              </Link>
              <Link
                href="/promotions"
                className="flex items-center gap-2 px-3 py-2 rounded-md hover:bg-accent transition-colors"
              >
                <span>🚀</span> Promotions
              </Link>
              <Link
                href="/gates"
                className="flex items-center gap-2 px-3 py-2 rounded-md hover:bg-accent transition-colors"
              >
                <span>🚦</span> Gates
              </Link>
            </nav>
            <div className="p-4 border-t border-border text-xs text-muted-foreground">
              v0.1.0 · Phase 1
            </div>
          </aside>

          {/* Main content */}
          <main className="flex-1 overflow-auto bg-background">
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
