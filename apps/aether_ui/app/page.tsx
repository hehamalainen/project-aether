"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

// API base URL - uses environment variable or defaults to localhost
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8100";

interface Stats {
  repositories: number;
  pendingProposals: number;
  promotionsTotal: number;
  activeAgents: Set<string>;
}

interface Proposal {
  id: string;
  agent_id: string;
  task_id: string;
  status: string;
  confidence_score: number;
  submitted_at: string;
}

interface Repo {
  id: string;
  name: string;
}

function StatusBadge({ status }: { status: string }) {
  const variants: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
    PENDING: "secondary",
    EVALUATING: "outline",
    PROMOTED: "default",
    REJECTED: "destructive",
    QUARANTINED: "destructive",
  };
  const colors: Record<string, string> = {
    PENDING: "",
    EVALUATING: "border-yellow-500 text-yellow-500",
    PROMOTED: "bg-green-600",
    REJECTED: "",
    QUARANTINED: "bg-orange-600",
  };
  return (
    <Badge variant={variants[status] || "secondary"} className={colors[status]}>
      {status}
    </Badge>
  );
}

function formatTimeAgo(dateString: string): string {
  const date = new Date(dateString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins} min ago`;
  const diffHrs = Math.floor(diffMins / 60);
  if (diffHrs < 24) return `${diffHrs} hr ago`;
  const diffDays = Math.floor(diffHrs / 24);
  return `${diffDays} day ago`;
}

export default function Dashboard() {
  const [stats, setStats] = useState({
    repositories: 0,
    pendingProposals: 0,
    promotionsTotal: 0,
    activeAgents: 0,
  });
  const [recentProposals, setRecentProposals] = useState<Proposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchDashboardData() {
      try {
        // Fetch repos
        const reposResponse = await fetch(`${API_BASE_URL}/v1/repos`);
        const repos: Repo[] = reposResponse.ok ? await reposResponse.json() : [];

        // Fetch proposals from all repos
        let allProposals: Proposal[] = [];
        const agentSet = new Set<string>();

        for (const repo of repos) {
          const proposalsResponse = await fetch(`${API_BASE_URL}/v1/repos/${repo.id}/proposals`);
          if (proposalsResponse.ok) {
            const data = await proposalsResponse.json();
            const proposals = data.proposals || [];
            allProposals = [...allProposals, ...proposals];
            proposals.forEach((p: Proposal) => agentSet.add(p.agent_id));
          }
        }

        // Sort by submission time (most recent first)
        allProposals.sort((a, b) =>
          new Date(b.submitted_at).getTime() - new Date(a.submitted_at).getTime()
        );

        // Compute stats
        const pendingCount = allProposals.filter(p => p.status === "PENDING").length;
        const promotedCount = allProposals.filter(p => p.status === "PROMOTED").length;

        setStats({
          repositories: repos.length,
          pendingProposals: pendingCount,
          promotionsTotal: promotedCount,
          activeAgents: agentSet.size,
        });

        // Take most recent 5 for display
        setRecentProposals(allProposals.slice(0, 5));
      } catch (err) {
        console.error("Failed to fetch dashboard data:", err);
        setError(err instanceof Error ? err.message : "Failed to load");
      } finally {
        setLoading(false);
      }
    }

    fetchDashboardData();

    // Refresh every 10 seconds
    const interval = setInterval(fetchDashboardData, 10000);
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return (
      <div className="p-8">
        <h1 className="text-3xl font-bold mb-4">Dashboard</h1>
        <p className="text-muted-foreground">Loading Aether data...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8">
        <h1 className="text-3xl font-bold mb-4">Dashboard</h1>
        <p className="text-red-500">Error: {error}</p>
        <p className="text-muted-foreground mt-2">Make sure Aether API is running at {API_BASE_URL}</p>
      </div>
    );
  }

  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="text-3xl font-bold">Dashboard</h1>
        <p className="text-muted-foreground mt-1">
          Real-time overview of your agent swarm activity
        </p>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Repositories</CardDescription>
            <CardTitle className="text-4xl">{stats.repositories}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Pending Proposals</CardDescription>
            <CardTitle className="text-4xl text-yellow-500">{stats.pendingProposals}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Total Promoted</CardDescription>
            <CardTitle className="text-4xl text-green-500">{stats.promotionsTotal}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Active Agents</CardDescription>
            <CardTitle className="text-4xl text-blue-500">{stats.activeAgents}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      {/* Recent Activity */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent Proposals */}
        <Card>
          <CardHeader>
            <CardTitle>Recent Proposals</CardTitle>
            <CardDescription>Latest agent submissions from MX Swarm</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {recentProposals.length === 0 ? (
                <p className="text-muted-foreground text-sm">No proposals yet</p>
              ) : (
                recentProposals.map((proposal) => (
                  <div
                    key={proposal.id}
                    className="flex items-center justify-between p-3 rounded-lg bg-muted/50"
                  >
                    <div className="flex-1">
                      <div className="font-mono text-sm">{proposal.id}</div>
                      <div className="text-xs text-muted-foreground">
                        {proposal.agent_id} · Task: {proposal.task_id} · {(proposal.confidence_score * 100).toFixed(0)}%
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-muted-foreground">
                        {formatTimeAgo(proposal.submitted_at)}
                      </span>
                      <StatusBadge status={proposal.status} />
                    </div>
                  </div>
                ))
              )}
            </div>
          </CardContent>
        </Card>

        {/* System Status */}
        <Card>
          <CardHeader>
            <CardTitle>System Status</CardTitle>
            <CardDescription>Aether VCS connections</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <div className="flex items-center justify-between p-3 rounded-lg bg-muted/50">
                <div className="flex items-center gap-3">
                  <span className="relative flex h-3 w-3">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
                    <span className="relative inline-flex rounded-full h-3 w-3 bg-green-500"></span>
                  </span>
                  <span>Aether Backend</span>
                </div>
                <Badge variant="default" className="bg-green-600">Connected</Badge>
              </div>
              <div className="flex items-center justify-between p-3 rounded-lg bg-muted/50">
                <div className="flex items-center gap-3">
                  <span className="relative flex h-3 w-3">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
                    <span className="relative inline-flex rounded-full h-3 w-3 bg-green-500"></span>
                  </span>
                  <span>MX Swarm Integration</span>
                </div>
                <Badge variant="default" className="bg-green-600">Active</Badge>
              </div>
              <div className="text-xs text-muted-foreground mt-4">
                API: {API_BASE_URL}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Live Activity Indicator */}
      <div className="mt-8 flex items-center gap-2 text-sm text-muted-foreground">
        <span className="relative flex h-3 w-3">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
          <span className="relative inline-flex rounded-full h-3 w-3 bg-green-500"></span>
        </span>
        Live · Auto-refreshing every 10 seconds
      </div>
    </div>
  );
}
