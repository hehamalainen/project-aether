"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8100";

interface Promotion {
    id: string;
    from_sequence: number;
    to_sequence: number;
    final_score: number;
    triggered_by: string;
    created_at: string;
}

interface PromotionDetail {
    id: string;
    repo_id: string;
    included_proposals: string[] | string;
    evidence: Record<string, string>;
}

interface Proposal {
    id: string;
    agent_id: string;
    task_id: string;
    change_content: string;
    affected_paths: string[];
}

interface Repo {
    id: string;
    name: string;
}

function formatTimeAgo(dateString: string): string {
    // API returns UTC timestamps without Z suffix, so add it
    const utcString = dateString.endsWith('Z') ? dateString : dateString + 'Z';
    const date = new Date(utcString);
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
}

interface DiffModalProps {
    promotion: Promotion;
    repoId: string;
    repoName: string;
    onClose: () => void;
}

function DiffModal({ promotion, repoId, repoName, onClose }: DiffModalProps) {
    const [proposals, setProposals] = useState<Proposal[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        async function fetchDiffs() {
            try {
                // Fetch promotion details to get included_proposals
                const detailRes = await fetch(`${API_BASE_URL}/v1/repos/${repoId}/promotions/${promotion.id}`);
                if (!detailRes.ok) throw new Error("Failed to fetch promotion details");
                const detail: PromotionDetail = await detailRes.json();

                // Parse included_proposals - it may be a JSON string or array
                let proposalIds: string[] = [];
                if (typeof detail.included_proposals === 'string') {
                    try {
                        proposalIds = JSON.parse(detail.included_proposals);
                    } catch {
                        proposalIds = [];
                    }
                } else if (Array.isArray(detail.included_proposals)) {
                    proposalIds = detail.included_proposals;
                }

                if (proposalIds.length === 0) {
                    setProposals([]);
                    setLoading(false);
                    return;
                }

                // Fetch each included proposal
                const proposalPromises = proposalIds.map(async (propId) => {
                    const propRes = await fetch(`${API_BASE_URL}/v1/repos/${repoId}/proposals/${propId}`);
                    if (propRes.ok) {
                        return propRes.json();
                    }
                    return null;
                });

                const fetchedProposals = (await Promise.all(proposalPromises)).filter(Boolean) as Proposal[];
                setProposals(fetchedProposals);
            } catch (err) {
                console.error("Failed to fetch diffs:", err);
                setError(err instanceof Error ? err.message : "Failed to load diffs");
            } finally {
                setLoading(false);
            }
        }
        fetchDiffs();
    }, [promotion.id, repoId]);

    return (
        <div className="fixed inset-0 bg-background/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
            <Card className="w-full max-w-4xl max-h-[80vh] flex flex-col">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <div>
                        <CardTitle className="text-xl">
                            Promotion: head_{promotion.from_sequence} → head_{promotion.to_sequence}
                        </CardTitle>
                        <CardDescription>
                            {repoName} · {formatTimeAgo(promotion.created_at)}
                        </CardDescription>
                    </div>
                    <Button variant="ghost" size="sm" onClick={onClose}>
                        Close
                    </Button>
                </CardHeader>
                <CardContent className="overflow-auto pt-4 flex-1">
                    {loading ? (
                        <p className="text-muted-foreground">Loading diffs...</p>
                    ) : error ? (
                        <p className="text-red-500">{error}</p>
                    ) : proposals.length === 0 ? (
                        <p className="text-muted-foreground">No proposals found for this promotion</p>
                    ) : (
                        <div className="space-y-4">
                            {proposals.map((proposal) => (
                                <div key={proposal.id} className="border rounded-lg p-4">
                                    <div className="flex items-center justify-between mb-2">
                                        <div>
                                            <span className="font-mono text-sm">{proposal.id}</span>
                                            <span className="text-muted-foreground text-sm ml-2">
                                                by {proposal.agent_id}
                                            </span>
                                        </div>
                                        {proposal.task_id && (
                                            <Badge variant="outline" className="text-xs">
                                                {proposal.task_id}
                                            </Badge>
                                        )}
                                    </div>
                                    <pre className="p-4 rounded bg-muted font-mono text-xs overflow-x-auto whitespace-pre">
                                        {proposal.change_content || "No diff content available"}
                                    </pre>
                                </div>
                            ))}
                        </div>
                    )}
                </CardContent>
            </Card>
        </div>
    );
}

export default function PromotionsPage() {
    const [promotions, setPromotions] = useState<{ promotion: Promotion, repoName: string, repoId: string }[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [selectedPromotion, setSelectedPromotion] = useState<{ promotion: Promotion, repoName: string, repoId: string } | null>(null);

    useEffect(() => {
        async function fetchPromotions() {
            try {
                // First get all repos
                const reposRes = await fetch(`${API_BASE_URL}/v1/repos`);
                if (!reposRes.ok) throw new Error("Failed to fetch repos");
                const repos: Repo[] = await reposRes.json();

                // Fetch promotions from each repo
                const allPromotions: { promotion: Promotion, repoName: string, repoId: string }[] = [];
                for (const repo of repos) {
                    const promRes = await fetch(`${API_BASE_URL}/v1/repos/${repo.id}/promotions`);
                    if (promRes.ok) {
                        const data = await promRes.json();
                        for (const prom of data.promotions || []) {
                            allPromotions.push({ promotion: prom, repoName: repo.name, repoId: repo.id });
                        }
                    }
                }

                // Sort by created_at descending
                allPromotions.sort((a, b) =>
                    new Date(b.promotion.created_at).getTime() - new Date(a.promotion.created_at).getTime()
                );

                setPromotions(allPromotions);
            } catch (err) {
                console.error("Failed to fetch promotions:", err);
                setError(err instanceof Error ? err.message : "Failed to load promotions");
            } finally {
                setLoading(false);
            }
        }
        fetchPromotions();
    }, []);

    if (loading) {
        return (
            <div className="p-8">
                <h1 className="text-3xl font-bold mb-4">Promotions</h1>
                <p className="text-muted-foreground">Loading promotions...</p>
            </div>
        );
    }

    if (error) {
        return (
            <div className="p-8">
                <h1 className="text-3xl font-bold mb-4">Promotions</h1>
                <p className="text-red-500">Error: {error}</p>
            </div>
        );
    }

    return (
        <div className="p-8">
            {selectedPromotion && (
                <DiffModal
                    promotion={selectedPromotion.promotion}
                    repoId={selectedPromotion.repoId}
                    repoName={selectedPromotion.repoName}
                    onClose={() => setSelectedPromotion(null)}
                />
            )}

            <div className="mb-8">
                <h1 className="text-3xl font-bold">Promotions</h1>
                <p className="text-muted-foreground mt-1">
                    History of canonical head advancements
                </p>
            </div>

            {promotions.length === 0 ? (
                <Card>
                    <CardContent className="pt-6">
                        <p className="text-muted-foreground text-center">No promotions yet</p>
                    </CardContent>
                </Card>
            ) : (
                <div className="space-y-4">
                    {promotions.map(({ promotion: prom, repoName, repoId }) => (
                        <Card key={prom.id}>
                            <CardHeader>
                                <div className="flex items-center justify-between">
                                    <div>
                                        <CardTitle className="text-lg font-mono">{prom.id}</CardTitle>
                                        <CardDescription>
                                            {repoName} · head_{prom.from_sequence} → head_{prom.to_sequence}
                                        </CardDescription>
                                    </div>
                                    <div className="text-right">
                                        <div className="text-2xl font-bold text-green-500">
                                            {(prom.final_score * 100).toFixed(0)}%
                                        </div>
                                        <div className="text-xs text-muted-foreground">
                                            {formatTimeAgo(prom.created_at)}
                                        </div>
                                    </div>
                                </div>
                            </CardHeader>
                            <CardContent>
                                <div className="flex items-center justify-between">
                                    <div className="flex items-center gap-4 text-sm text-muted-foreground">
                                        <span>
                                            Triggered by: <span className="font-mono">{prom.triggered_by}</span>
                                        </span>
                                        <Badge variant="outline" className="bg-green-600/10 text-green-500 border-green-500/30">
                                            PROMOTED
                                        </Badge>
                                    </div>
                                    <Button
                                        variant="ghost"
                                        size="sm"
                                        onClick={() => setSelectedPromotion({ promotion: prom, repoName, repoId })}
                                    >
                                        View Diff
                                    </Button>
                                </div>
                            </CardContent>
                        </Card>
                    ))}
                </div>
            )}
        </div>
    );
}
