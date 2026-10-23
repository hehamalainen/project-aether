"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";

// Mock gate results
const gateResults = [
    {
        id: "gate_001",
        snapshot_id: "snap_156",
        proposal_id: "prop_003",
        gate_name: "syntax",
        status: "PASS",
        duration_ms: 125,
        exit_code: 0,
    },
    {
        id: "gate_002",
        snapshot_id: "snap_156",
        proposal_id: "prop_003",
        gate_name: "build",
        status: "PASS",
        duration_ms: 4521,
        exit_code: 0,
    },
    {
        id: "gate_003",
        snapshot_id: "snap_156",
        proposal_id: "prop_003",
        gate_name: "unit_tests",
        status: "PASS",
        duration_ms: 12340,
        tests_passed: 142,
        tests_failed: 0,
        exit_code: 0,
    },
    {
        id: "gate_004",
        snapshot_id: "snap_156",
        proposal_id: "prop_003",
        gate_name: "lint",
        status: "PASS",
        duration_ms: 890,
        exit_code: 0,
    },
    {
        id: "gate_005",
        snapshot_id: "snap_155",
        proposal_id: "prop_004",
        gate_name: "unit_tests",
        status: "FAIL",
        duration_ms: 8900,
        tests_passed: 138,
        tests_failed: 4,
        exit_code: 1,
    },
];

// Stats
const stats = {
    totalRuns: 156,
    passRate: 94.2,
    avgDuration: 4.2,
    failedToday: 3,
};

function StatusBadge({ status }: { status: string }) {
    const colors: Record<string, string> = {
        PASS: "bg-green-600 text-white",
        FAIL: "bg-red-600 text-white",
        PENDING: "bg-yellow-600 text-white",
        RUNNING: "bg-blue-600 text-white",
        SKIP: "bg-gray-600 text-white",
        ERROR: "bg-orange-600 text-white",
    };
    return (
        <span className={`px-2 py-0.5 rounded text-xs font-medium ${colors[status] || "bg-gray-600"}`}>
            {status}
        </span>
    );
}

function formatDuration(ms: number): string {
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${(ms / 60000).toFixed(1)}m`;
}

export default function GatesPage() {
    return (
        <div className="p-8">
            <div className="mb-8">
                <h1 className="text-3xl font-bold">Gates</h1>
                <p className="text-muted-foreground mt-1">
                    Verification gate execution results
                </p>
            </div>

            {/* Stats */}
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
                <Card>
                    <CardHeader className="pb-2">
                        <CardDescription>Total Runs</CardDescription>
                        <CardTitle className="text-3xl">{stats.totalRuns}</CardTitle>
                    </CardHeader>
                </Card>
                <Card>
                    <CardHeader className="pb-2">
                        <CardDescription>Pass Rate</CardDescription>
                        <CardTitle className="text-3xl text-green-500">{stats.passRate}%</CardTitle>
                    </CardHeader>
                </Card>
                <Card>
                    <CardHeader className="pb-2">
                        <CardDescription>Avg Duration</CardDescription>
                        <CardTitle className="text-3xl">{stats.avgDuration}s</CardTitle>
                    </CardHeader>
                </Card>
                <Card>
                    <CardHeader className="pb-2">
                        <CardDescription>Failed Today</CardDescription>
                        <CardTitle className="text-3xl text-red-500">{stats.failedToday}</CardTitle>
                    </CardHeader>
                </Card>
            </div>

            {/* Recent Results */}
            <Card>
                <CardHeader>
                    <CardTitle>Recent Gate Results</CardTitle>
                    <CardDescription>Latest verification runs</CardDescription>
                </CardHeader>
                <CardContent>
                    <Table>
                        <TableHeader>
                            <TableRow>
                                <TableHead>ID</TableHead>
                                <TableHead>Gate</TableHead>
                                <TableHead>Proposal</TableHead>
                                <TableHead>Status</TableHead>
                                <TableHead>Duration</TableHead>
                                <TableHead>Tests</TableHead>
                                <TableHead>Exit Code</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {gateResults.map((result) => (
                                <TableRow key={result.id}>
                                    <TableCell className="font-mono text-xs">{result.id}</TableCell>
                                    <TableCell>
                                        <Badge variant="outline">{result.gate_name}</Badge>
                                    </TableCell>
                                    <TableCell className="font-mono text-xs">{result.proposal_id}</TableCell>
                                    <TableCell>
                                        <StatusBadge status={result.status} />
                                    </TableCell>
                                    <TableCell className="font-mono text-sm">
                                        {formatDuration(result.duration_ms)}
                                    </TableCell>
                                    <TableCell>
                                        {result.tests_passed !== undefined ? (
                                            <span>
                                                <span className="text-green-500">{result.tests_passed}</span>
                                                {result.tests_failed ? (
                                                    <span className="text-red-500"> / {result.tests_failed}</span>
                                                ) : null}
                                            </span>
                                        ) : (
                                            <span className="text-muted-foreground">-</span>
                                        )}
                                    </TableCell>
                                    <TableCell className="font-mono">
                                        {result.exit_code === 0 ? (
                                            <span className="text-green-500">0</span>
                                        ) : (
                                            <span className="text-red-500">{result.exit_code}</span>
                                        )}
                                    </TableCell>
                                </TableRow>
                            ))}
                        </TableBody>
                    </Table>
                </CardContent>
            </Card>
        </div>
    );
}
