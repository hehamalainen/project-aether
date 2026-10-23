"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Plus, Loader2 } from "lucide-react";
import Link from "next/link";
import { aetherApi, Repository } from "@/lib/api";

export default function RepositoriesPage() {
    const [repos, setRepos] = useState<Repository[]>([]);
    const [loading, setLoading] = useState(true);
    const [isCreating, setIsCreating] = useState(false);
    const [newName, setNewName] = useState("");
    const [newDesc, setNewDesc] = useState("");
    const [isSubmitting, setIsSubmitting] = useState(false);

    useEffect(() => {
        loadRepos();
    }, []);

    const loadRepos = async () => {
        try {
            setLoading(true);
            const data = await aetherApi.listRepos();
            setRepos(data);
        } catch (error) {
            console.error("Failed to load repos:", error);
        } finally {
            setLoading(false);
        }
    };

    const handleCreate = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!newName) return;

        try {
            setIsSubmitting(true);
            await aetherApi.createRepo({ name: newName, description: newDesc });
            setNewName("");
            setNewDesc("");
            setIsCreating(false);
            await loadRepos();
        } catch (error) {
            console.error("Failed to create repo:", error);
            alert("Failed to create repository. Is the backend running?");
        } finally {
            setIsSubmitting(false);
        }
    };

    return (
        <div className="p-8 max-w-7xl mx-auto">
            <div className="flex items-center justify-between mb-8">
                <div>
                    <h1 className="text-4xl font-extrabold tracking-tight">Repositories</h1>
                    <p className="text-muted-foreground mt-2 text-lg">
                        Manage your Agent-Native version control repositories
                    </p>
                </div>
                <Button
                    onClick={() => setIsCreating(true)}
                    className="bg-blue-600 hover:bg-blue-700 text-white gap-2 px-6 py-6 text-lg rounded-xl shadow-lg shadow-blue-500/20 transition-all hover:scale-105 active:scale-95"
                >
                    <Plus className="w-5 h-5" />
                    New Repository
                </Button>
            </div>

            {isCreating && (
                <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
                    <Card className="w-full max-w-md shadow-2xl border-blue-500/20 bg-background/95">
                        <CardHeader>
                            <CardTitle>Create New Repository</CardTitle>
                            <CardDescription>Initialize a new Aether repository for your agents.</CardDescription>
                        </CardHeader>
                        <CardContent>
                            <form onSubmit={handleCreate} className="space-y-4">
                                <div className="space-y-2">
                                    <label className="text-sm font-medium">Repository Name</label>
                                    <input
                                        autoFocus
                                        value={newName}
                                        onChange={(e) => setNewName(e.target.value)}
                                        placeholder="e.g., backend-api"
                                        className="w-full p-3 rounded-lg border bg-background focus:ring-2 focus:ring-blue-500 outline-none transition-all"
                                    />
                                </div>
                                <div className="space-y-2">
                                    <label className="text-sm font-medium">Description</label>
                                    <textarea
                                        value={newDesc}
                                        onChange={(e) => setNewDesc(e.target.value)}
                                        placeholder="What is this repository for?"
                                        className="w-full p-3 rounded-lg border bg-background focus:ring-2 focus:ring-blue-500 outline-none transition-all h-24"
                                    />
                                </div>
                                <div className="flex gap-3 justify-end pt-4">
                                    <Button
                                        type="button"
                                        variant="outline"
                                        onClick={() => setIsCreating(false)}
                                        disabled={isSubmitting}
                                    >
                                        Cancel
                                    </Button>
                                    <Button
                                        type="submit"
                                        className="bg-blue-600 hover:bg-blue-700"
                                        disabled={!newName || isSubmitting}
                                    >
                                        {isSubmitting ? <Loader2 className="w-4 h-4 animate-spin" /> : "Create Repository"}
                                    </Button>
                                </div>
                            </form>
                        </CardContent>
                    </Card>
                </div>
            )}

            {loading ? (
                <div className="flex items-center justify-center h-64">
                    <Loader2 className="w-12 h-12 animate-spin text-blue-500" />
                </div>
            ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                    {repos.length === 0 ? (
                        <div className="col-span-full py-20 text-center border-2 border-dashed rounded-3xl border-muted-foreground/20">
                            <Plus className="w-12 h-12 mx-auto text-muted-foreground/30 mb-4" />
                            <h3 className="text-xl font-semibold">No repositories found</h3>
                            <p className="text-muted-foreground mt-2">Create your first repository to get started with Project Aether.</p>
                        </div>
                    ) : (
                        repos.map((repo) => (
                            <Link key={repo.id} href={`/repos/${repo.id}`}>
                                <Card className="group hover:border-blue-500/50 hover:shadow-xl hover:shadow-blue-500/10 transition-all cursor-pointer h-full rounded-2xl border-muted/50 overflow-hidden">
                                    <CardHeader className="bg-muted/30 group-hover:bg-blue-500/5 transition-colors">
                                        <div className="flex items-center justify-between">
                                            <CardTitle className="text-xl font-bold">{repo.name}</CardTitle>
                                            <Badge variant="outline" className="bg-background">Auto</Badge>
                                        </div>
                                        <CardDescription className="line-clamp-2 mt-2">{repo.description || "No description provided."}</CardDescription>
                                    </CardHeader>
                                    <CardContent className="pt-6">
                                        <div className="space-y-4 text-sm">
                                            <div className="flex justify-between items-center bg-muted/50 p-2 rounded-lg">
                                                <span className="text-muted-foreground font-medium">Head Sequence</span>
                                                <span className="font-mono text-blue-500 font-bold">{repo.head_sequence}</span>
                                            </div>
                                            <div className="flex justify-between items-center px-1">
                                                <span className="text-muted-foreground">ID</span>
                                                <span className="font-mono text-xs opacity-50">{repo.id}</span>
                                            </div>
                                            <div className="flex justify-between items-center px-1">
                                                <span className="text-muted-foreground">Created</span>
                                                <span>{new Date(repo.created_at).toLocaleDateString()}</span>
                                            </div>
                                        </div>
                                    </CardContent>
                                </Card>
                            </Link>
                        ))
                    )}
                </div>
            )}
        </div>
    );
}
