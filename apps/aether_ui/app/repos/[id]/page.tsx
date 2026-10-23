"use client";

import { use, useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import Link from "next/link";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8100";

interface RepoData {
    id: string;
    name: string;
    description: string;
    head_id: string | null;
    head_sequence: number;
    created_at: string;
    updated_at: string;
}

interface HeadData {
    head_id: string;
    sequence: number;
    snapshot_id: string;
    content_hash: string;
    last_promotion_id: string | null;
    last_promotion_at: string | null;
}

interface SnapshotData {
    id: string;
    content_hash: string;
    file_count: number;
    total_bytes: number;
    files: string[];
}

interface FileContent {
    path: string;
    content: string;
}

function formatTimeAgo(dateString: string): string {
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

export default function RepoDetailPage({ params }: { params: Promise<{ id: string }> }) {
    const { id } = use(params);
    const [repo, setRepo] = useState<RepoData | null>(null);
    const [head, setHead] = useState<HeadData | null>(null);
    const [files, setFiles] = useState<string[]>([]);
    const [pendingCount, setPendingCount] = useState(0);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [selectedFile, setSelectedFile] = useState<FileContent | null>(null);
    const [loadingFile, setLoadingFile] = useState(false);

    useEffect(() => {
        async function fetchRepoData() {
            try {
                // Fetch repo info
                const repoRes = await fetch(`${API_BASE_URL}/v1/repos/${id}`);
                if (!repoRes.ok) throw new Error("Repository not found");
                const repoData: RepoData = await repoRes.json();
                setRepo(repoData);

                // Fetch head info
                const headRes = await fetch(`${API_BASE_URL}/v1/repos/${id}/head`);
                if (headRes.ok) {
                    const headData: HeadData = await headRes.json();
                    setHead(headData);

                    // Fetch files from snapshot
                    if (headData.snapshot_id) {
                        const snapRes = await fetch(`${API_BASE_URL}/v1/repos/${id}/snapshots/${headData.snapshot_id}`);
                        if (snapRes.ok) {
                            const snapData: SnapshotData = await snapRes.json();
                            setFiles(snapData.files || []);
                        }
                    }
                }

                // Fetch pending proposals count
                const propsRes = await fetch(`${API_BASE_URL}/v1/repos/${id}/proposals?status=PENDING`);
                if (propsRes.ok) {
                    const propsData = await propsRes.json();
                    setPendingCount(propsData.total || 0);
                }
            } catch (err) {
                console.error("Failed to fetch repo:", err);
                setError(err instanceof Error ? err.message : "Failed to load repository");
            } finally {
                setLoading(false);
            }
        }
        fetchRepoData();
    }, [id]);

    async function viewFile(filePath: string) {
        if (!head?.snapshot_id) return;
        setLoadingFile(true);
        try {
            const res = await fetch(`${API_BASE_URL}/v1/repos/${id}/snapshots/${head.snapshot_id}/files/${filePath}`);
            if (res.ok) {
                const data = await res.json();
                setSelectedFile({ path: filePath, content: data.content });
            }
        } catch (err) {
            console.error("Failed to load file:", err);
        } finally {
            setLoadingFile(false);
        }
    }

    if (loading) {
        return (
            <div className="p-8">
                <p className="text-muted-foreground">Loading repository...</p>
            </div>
        );
    }

    if (error || !repo) {
        return (
            <div className="p-8">
                <Link href="/repos">
                    <Button variant="outline" size="sm">← Back</Button>
                </Link>
                <p className="text-red-500 mt-4">{error || "Repository not found"}</p>
            </div>
        );
    }

    return (
        <div className="p-8">
            {/* File Viewer Modal */}
            {selectedFile && (
                <div className="fixed inset-0 bg-background/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
                    <Card className="w-full max-w-5xl max-h-[85vh] flex flex-col">
                        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                            <div>
                                <CardTitle className="text-lg font-mono">{selectedFile.path}</CardTitle>
                                <CardDescription>
                                    Snapshot: {head?.snapshot_id}
                                </CardDescription>
                            </div>
                            <Button variant="ghost" size="sm" onClick={() => setSelectedFile(null)}>
                                Close
                            </Button>
                        </CardHeader>
                        <CardContent className="overflow-auto pt-4 flex-1">
                            <pre className="p-4 rounded bg-muted font-mono text-xs overflow-x-auto whitespace-pre">
                                {selectedFile.content || "(empty file)"}
                            </pre>
                        </CardContent>
                    </Card>
                </div>
            )}

            <div className="flex items-center gap-4 mb-8">
                <Link href="/repos">
                    <Button variant="outline" size="sm">← Back</Button>
                </Link>
                <div>
                    <div className="flex items-center gap-2">
                        <h1 className="text-3xl font-bold">{repo.name}</h1>
                        <Badge variant="outline" className="font-mono">{id}</Badge>
                    </div>
                    <p className="text-muted-foreground mt-1">{repo.description}</p>
                </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
                <Card>
                    <CardHeader className="pb-2">
                        <CardDescription>Head Sequence</CardDescription>
                        <CardTitle className="text-2xl font-mono">{repo.head_sequence}</CardTitle>
                    </CardHeader>
                </Card>
                <Card>
                    <CardHeader className="pb-2">
                        <CardDescription>Files</CardDescription>
                        <CardTitle className="text-2xl text-blue-500">{files.length}</CardTitle>
                    </CardHeader>
                </Card>
                <Card>
                    <CardHeader className="pb-2">
                        <CardDescription>Pending Proposals</CardDescription>
                        <CardTitle className="text-2xl text-yellow-500">{pendingCount}</CardTitle>
                    </CardHeader>
                </Card>
                <Card>
                    <CardHeader className="pb-2">
                        <CardDescription>Last Promotion</CardDescription>
                        <CardTitle className="text-2xl">
                            {head?.last_promotion_at ? formatTimeAgo(head.last_promotion_at) : "Never"}
                        </CardTitle>
                    </CardHeader>
                </Card>
            </div>

            <Tabs defaultValue="files" className="space-y-4">
                <TabsList>
                    <TabsTrigger value="files">📁 Files ({files.length})</TabsTrigger>
                    <TabsTrigger value="info">ℹ️ Info</TabsTrigger>
                </TabsList>

                <TabsContent value="files">
                    <Card>
                        <CardHeader>
                            <CardTitle>Repository Files</CardTitle>
                            <CardDescription>
                                Current state at HEAD (sequence {repo.head_sequence})
                                {head?.content_hash && (
                                    <span className="ml-2 font-mono text-xs">
                                        hash: {head.content_hash.slice(0, 12)}...
                                    </span>
                                )}
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <div className="space-y-2">
                                {files.map((filePath) => (
                                    <div 
                                        key={filePath} 
                                        className="flex items-center justify-between p-3 rounded-md bg-muted/30 border border-border/50 hover:bg-muted/50 transition-colors cursor-pointer"
                                        onClick={() => viewFile(filePath)}
                                    >
                                        <div className="flex items-center gap-3">
                                            <span className="text-xl">📄</span>
                                            <span className="font-mono text-sm">{filePath}</span>
                                        </div>
                                        <Button variant="ghost" size="sm" disabled={loadingFile}>
                                            View
                                        </Button>
                                    </div>
                                ))}
                                {files.length === 0 && (
                                    <p className="text-center py-8 text-muted-foreground italic">
                                        {repo.head_sequence === 0 
                                            ? "No promotions yet. Submit proposals to add files."
                                            : "No files found in current HEAD snapshot."}
                                    </p>
                                )}
                            </div>
                        </CardContent>
                    </Card>
                </TabsContent>

                <TabsContent value="info">
                    <Card>
                        <CardHeader>
                            <CardTitle>Repository Information</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-2 gap-4 text-sm">
                                <div>
                                    <span className="text-muted-foreground">Repository ID:</span>
                                    <span className="ml-2 font-mono">{repo.id}</span>
                                </div>
                                <div>
                                    <span className="text-muted-foreground">Created:</span>
                                    <span className="ml-2">{new Date(repo.created_at).toLocaleDateString()}</span>
                                </div>
                                <div>
                                    <span className="text-muted-foreground">Head ID:</span>
                                    <span className="ml-2 font-mono">{head?.head_id || "None"}</span>
                                </div>
                                <div>
                                    <span className="text-muted-foreground">Snapshot ID:</span>
                                    <span className="ml-2 font-mono">{head?.snapshot_id || "None"}</span>
                                </div>
                                <div className="col-span-2">
                                    <span className="text-muted-foreground">Content Hash:</span>
                                    <span className="ml-2 font-mono text-xs">{head?.content_hash || "None"}</span>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                </TabsContent>
            </Tabs>
        </div>
    );
}
