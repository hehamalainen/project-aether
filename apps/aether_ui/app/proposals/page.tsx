"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";

// API base URL - uses environment variable or defaults to localhost
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8100";

interface Proposal {
    id: string;
    agent_id: string;
    task_id: string;
    status: string;
    confidence_score: number;
    submitted_at: string;
    change_type: string;
    change_content: string;
    affected_paths?: string[];
}

function formatTimeAgo(dateString: string): string {
    if (!dateString) return "unknown";
    try {
        // API returns UTC timestamps without Z suffix, so add it
        const utcString = dateString.endsWith('Z') ? dateString : dateString + 'Z';
        const date = new Date(utcString);
        if (isNaN(date.getTime())) return "unknown";

        const now = new Date();
        const diffMs = now.getTime() - date.getTime();
        const diffMins = Math.floor(diffMs / 60000);

        if (diffMins < 0) return "just now";
        if (diffMins < 1) return "just now";
        if (diffMins < 60) return `${diffMins} min ago`;
        const diffHrs = Math.floor(diffMins / 60);
        if (diffHrs < 24) return `${diffHrs} hr ago`;
        const diffDays = Math.floor(diffHrs / 24);
        return `${diffDays} day ago`;
    } catch (e) {
        return "unknown";
    }
}

function StatusBadge({ status }: { status: string }) {
    const variants: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
        PENDING: "secondary",
        EVALUATING: "outline",
        PROMOTED: "default",
        REJECTED: "destructive",
        QUARANTINED: "destructive",
        SUPERSEDED: "secondary",
    };
    const colors: Record<string, string> = {
        PENDING: "",
        EVALUATING: "border-yellow-500 text-yellow-500",
        PROMOTED: "bg-green-600",
        REJECTED: "",
        QUARANTINED: "bg-orange-600",
        SUPERSEDED: "",
    };
    return (
        <Badge variant={variants[status] || "secondary"} className={colors[status]}>
            {status}
        </Badge>
    );
}

function ConfidenceBadge({ confidence }: { confidence: number }) {
    const color = confidence >= 0.9 ? "text-green-500" : confidence >= 0.8 ? "text-yellow-500" : "text-red-500";
    return (
        <span className={`font-mono ${color}`}>
            {(confidence * 100).toFixed(0)}%
        </span>
    );
}

function DiffModal({ proposal, onClose }: { proposal: Proposal; onClose: () => void }) {
    return (
        <div className="fixed inset-0 bg-background/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
            <Card className="w-full max-w-4xl max-h-[80vh] flex flex-col">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <div>
                        <CardTitle className="text-xl font-mono">{proposal.id}</CardTitle>
                        <CardDescription>
                            Agent: {proposal.agent_id} | Task: {proposal.task_id}
                        </CardDescription>
                    </div>
                    <Button variant="ghost" size="sm" onClick={onClose}>
                        Close
                    </Button>
                </CardHeader>
                <CardContent className="overflow-auto pt-4 flex-1">
                    <pre className="p-4 rounded bg-muted font-mono text-xs overflow-x-auto whitespace-pre">
                        {proposal.change_content}
                    </pre>
                </CardContent>
            </Card>
        </div>
    );
}

export default function ProposalsPage() {
    const [proposals, setProposals] = useState<Proposal[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [selectedProposal, setSelectedProposal] = useState<Proposal | null>(null);

    useEffect(() => {
        async function fetchProposals() {
            try {
                // Fetch all repos first
                const reposRes = await fetch(`${API_BASE_URL}/v1/repos`);
                if (!reposRes.ok) throw new Error("Failed to fetch repos");
                const repos = await reposRes.json();

                // Fetch proposals from all repos
                const allProposals: Proposal[] = [];
                for (const repo of repos) {
                    const response = await fetch(`${API_BASE_URL}/v1/repos/${repo.id}/proposals?limit=200`);
                    if (response.ok) {
                        const data = await response.json();
                        allProposals.push(...(data.proposals || []));
                    }
                }

                // Sort by submitted_at descending (newest first)
                allProposals.sort((a, b) =>
                    new Date(b.submitted_at).getTime() - new Date(a.submitted_at).getTime()
                );

                setProposals(allProposals);
            } catch (err) {
                console.error("Failed to fetch proposals:", err);
                setError(err instanceof Error ? err.message : "Failed to load proposals");
            } finally {
                setLoading(false);
            }
        }
        fetchProposals();

        // Refresh every 5 seconds to catch pipeline updates
        const interval = setInterval(fetchProposals, 5000);
        return () => clearInterval(interval);
    }, []);

    if (loading) {
        return (
            <div className="p-8">
                <h1 className="text-3xl font-bold mb-4">Proposals</h1>
                <p className="text-muted-foreground">Loading proposals...</p>
            </div>
        );
    }

    if (error) {
        return (
            <div className="p-8 flex flex-col items-center justify-center min-h-[50vh]">
                <h1 className="text-3xl font-bold mb-4">Proposals</h1>
                <Card className="w-full max-w-md border-destructive">
                    <CardHeader>
                        <CardTitle className="text-destructive">Load Failed</CardTitle>
                        <CardDescription>
                            Make sure the Aether API is running at {API_BASE_URL}
                        </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <p className="text-sm text-muted-foreground">
                            Error: {error}
                        </p>
                        <Button
                            className="w-full"
                            onClick={() => {
                                setError(null);
                                setLoading(true);
                                // The interval/useEffect will trigger a re-fetch
                                window.location.reload();
                            }}
                        >
                            Retry Connection
                        </Button>
                    </CardContent>
                </Card>
            </div>
        );
    }

    return (
        <div className="p-8">
            {selectedProposal && (
                <DiffModal
                    proposal={selectedProposal}
                    onClose={() => setSelectedProposal(null)}
                />
            )}

            <div className="flex items-center justify-between mb-8">
                <div>
                    <h1 className="text-3xl font-bold">Proposals</h1>
                    <p className="text-muted-foreground mt-1">
                        Agent submissions across all repositories
                    </p>
                </div>
            </div>

            <Tabs defaultValue="all" className="space-y-4">
                <TabsList>
                    <TabsTrigger value="all">All</TabsTrigger>
                    <TabsTrigger value="pending">Pending</TabsTrigger>
                    <TabsTrigger value="evaluating">Evaluating</TabsTrigger>
                    <TabsTrigger value="promoted">Promoted</TabsTrigger>
                    <TabsTrigger value="rejected">Rejected</TabsTrigger>
                </TabsList>

                <TabsContent value="all">
                    <Card>
                        <CardHeader>
                            <CardTitle>All Proposals</CardTitle>
                            <CardDescription>
                                {proposals.length} total proposals
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>ID</TableHead>
                                        <TableHead>Task ID</TableHead>
                                        <TableHead>Agent</TableHead>
                                        <TableHead>Confidence</TableHead>
                                        <TableHead>Status</TableHead>
                                        <TableHead>Submitted</TableHead>
                                        <TableHead></TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {proposals.map((proposal) => (
                                        <TableRow key={proposal.id}>
                                            <TableCell className="font-mono text-xs">{proposal.id}</TableCell>
                                            <TableCell className="font-mono text-xs">{proposal.task_id}</TableCell>
                                            <TableCell className="text-xs">{proposal.agent_id}</TableCell>
                                            <TableCell>
                                                <ConfidenceBadge confidence={proposal.confidence_score} />
                                            </TableCell>
                                            <TableCell>
                                                <StatusBadge status={proposal.status} />
                                            </TableCell>
                                            <TableCell className="text-muted-foreground text-sm">
                                                {formatTimeAgo(proposal.submitted_at)}
                                            </TableCell>
                                            <TableCell>
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    onClick={() => setSelectedProposal(proposal)}
                                                >
                                                    View Diff
                                                </Button>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </CardContent>
                    </Card>
                </TabsContent>

                <TabsContent value="pending">
                    <Card>
                        <CardHeader>
                            <CardTitle>Pending Proposals</CardTitle>
                            <CardDescription>
                                {proposals.filter(p => p.status === "PENDING").length} pending proposals
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>ID</TableHead>
                                        <TableHead>Task ID</TableHead>
                                        <TableHead>Agent</TableHead>
                                        <TableHead>Confidence</TableHead>
                                        <TableHead>Submitted</TableHead>
                                        <TableHead></TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {proposals.filter(p => p.status === "PENDING").map((proposal) => (
                                        <TableRow key={proposal.id}>
                                            <TableCell className="font-mono text-xs">{proposal.id}</TableCell>
                                            <TableCell className="font-mono text-xs">{proposal.task_id}</TableCell>
                                            <TableCell className="text-xs">{proposal.agent_id}</TableCell>
                                            <TableCell>
                                                <ConfidenceBadge confidence={proposal.confidence_score} />
                                            </TableCell>
                                            <TableCell className="text-muted-foreground text-sm">
                                                {formatTimeAgo(proposal.submitted_at)}
                                            </TableCell>
                                            <TableCell>
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    onClick={() => setSelectedProposal(proposal)}
                                                >
                                                    View Diff
                                                </Button>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </CardContent>
                    </Card>
                </TabsContent>

                <TabsContent value="evaluating">
                    <Card>
                        <CardHeader>
                            <CardTitle>Evaluating Proposals</CardTitle>
                            <CardDescription>
                                {proposals.filter(p => p.status === "EVALUATING").length} proposals being evaluated
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>ID</TableHead>
                                        <TableHead>Task ID</TableHead>
                                        <TableHead>Agent</TableHead>
                                        <TableHead>Confidence</TableHead>
                                        <TableHead>Submitted</TableHead>
                                        <TableHead></TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {proposals.filter(p => p.status === "EVALUATING").map((proposal) => (
                                        <TableRow key={proposal.id}>
                                            <TableCell className="font-mono text-xs">{proposal.id}</TableCell>
                                            <TableCell className="font-mono text-xs">{proposal.task_id}</TableCell>
                                            <TableCell className="text-xs">{proposal.agent_id}</TableCell>
                                            <TableCell>
                                                <ConfidenceBadge confidence={proposal.confidence_score} />
                                            </TableCell>
                                            <TableCell className="text-muted-foreground text-sm">
                                                {formatTimeAgo(proposal.submitted_at)}
                                            </TableCell>
                                            <TableCell>
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    onClick={() => setSelectedProposal(proposal)}
                                                >
                                                    View Diff
                                                </Button>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </CardContent>
                    </Card>
                </TabsContent>

                <TabsContent value="promoted">
                    <Card>
                        <CardHeader>
                            <CardTitle>Promoted Proposals</CardTitle>
                            <CardDescription>
                                {proposals.filter(p => p.status === "PROMOTED").length} promoted proposals
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>ID</TableHead>
                                        <TableHead>Task ID</TableHead>
                                        <TableHead>Agent</TableHead>
                                        <TableHead>Confidence</TableHead>
                                        <TableHead>Submitted</TableHead>
                                        <TableHead></TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {proposals.filter(p => p.status === "PROMOTED").map((proposal) => (
                                        <TableRow key={proposal.id}>
                                            <TableCell className="font-mono text-xs">{proposal.id}</TableCell>
                                            <TableCell className="font-mono text-xs">{proposal.task_id}</TableCell>
                                            <TableCell className="text-xs">{proposal.agent_id}</TableCell>
                                            <TableCell>
                                                <ConfidenceBadge confidence={proposal.confidence_score} />
                                            </TableCell>
                                            <TableCell className="text-muted-foreground text-sm">
                                                {formatTimeAgo(proposal.submitted_at)}
                                            </TableCell>
                                            <TableCell>
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    onClick={() => setSelectedProposal(proposal)}
                                                >
                                                    View Diff
                                                </Button>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </CardContent>
                    </Card>
                </TabsContent>

                <TabsContent value="rejected">
                    <Card>
                        <CardHeader>
                            <CardTitle>Rejected Proposals</CardTitle>
                            <CardDescription>
                                {proposals.filter(p => p.status === "REJECTED" || p.status === "QUARANTINED" || p.status === "SUPERSEDED").length} rejected/superseded proposals
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>ID</TableHead>
                                        <TableHead>Task ID</TableHead>
                                        <TableHead>Agent</TableHead>
                                        <TableHead>Confidence</TableHead>
                                        <TableHead>Status</TableHead>
                                        <TableHead>Submitted</TableHead>
                                        <TableHead></TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {proposals.filter(p => p.status === "REJECTED" || p.status === "QUARANTINED" || p.status === "SUPERSEDED").map((proposal) => (
                                        <TableRow key={proposal.id}>
                                            <TableCell className="font-mono text-xs">{proposal.id}</TableCell>
                                            <TableCell className="font-mono text-xs">{proposal.task_id}</TableCell>
                                            <TableCell className="text-xs">{proposal.agent_id}</TableCell>
                                            <TableCell>
                                                <ConfidenceBadge confidence={proposal.confidence_score} />
                                            </TableCell>
                                            <TableCell>
                                                <StatusBadge status={proposal.status} />
                                            </TableCell>
                                            <TableCell className="text-muted-foreground text-sm">
                                                {formatTimeAgo(proposal.submitted_at)}
                                            </TableCell>
                                            <TableCell>
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    onClick={() => setSelectedProposal(proposal)}
                                                >
                                                    View Diff
                                                </Button>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </CardContent>
                    </Card>
                </TabsContent>
            </Tabs>
        </div>
    );
}

