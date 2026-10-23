/**
 * Aether API Client
 */

const API_BASE = "http://localhost:8100/v1";

export interface Repository {
    id: string;
    name: string;
    description: string;
    head_id: string | null;
    head_sequence: number;
    created_at: string;
    updated_at: string;
}

export interface RepoCreateRequest {
    name: string;
    description?: string;
}

export const aetherApi = {
    /**
     * List all repositories
     */
    async listRepos(): Promise<Repository[]> {
        const response = await fetch(`${API_BASE}/repos`);
        if (!response.ok) {
            throw new Error(`Failed to list repos: ${response.statusText}`);
        }
        return response.json();
    },

    /**
     * Create a new repository
     */
    async createRepo(data: RepoCreateRequest): Promise<Repository> {
        const response = await fetch(`${API_BASE}/repos`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify(data),
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || `Failed to create repo: ${response.statusText}`);
        }
        return response.json();
    },

    /**
     * Get head information for a repository
     */
    async getHead(repoId: string) {
        const response = await fetch(`${API_BASE}/repos/${repoId}/head`);
        if (!response.ok) {
            throw new Error(`Failed to get head: ${response.statusText}`);
        }
        return response.json();
    },

    /**
     * List proposals for a repository
     */
    async listProposals(repoId: string, status?: string) {
        const url = new URL(`${API_BASE}/repos/${repoId}/proposals`);
        if (status) url.searchParams.append("status", status);

        const response = await fetch(url.toString());
        if (!response.ok) {
            throw new Error(`Failed to list proposals: ${response.statusText}`);
        }
        return response.json();
    }
};
