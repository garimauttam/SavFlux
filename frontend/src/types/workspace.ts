/**
 * types/workspace.ts — shapes for the two integrations the app gained.
 *
 * These mirror `backend/app/services/github_service.py` and
 * `model_service.py`. Kept in one file, with the optional fields declared as
 * optional rather than defaulted, because the honest thing to do with a value
 * GitHub did not send is to say so in the type — a `0` here would render as
 * "0 stars" on a repository the API merely omitted the count for.
 */

export interface GitHubUser {
  login: string;
  name?: string | null;
  avatar_url?: string | null;
  html_url?: string | null;
  type?: string | null;
}

export interface GitHubStatus {
  connected: boolean;
  valid: boolean;
  /** "app" = connected from the UI, "env" = GITHUB_TOKEN in the environment. */
  source?: "app" | "env" | null;
  user?: GitHubUser | null;
  message?: string | null;
  kind?: string;
}

export interface GitHubRepo {
  full_name: string;
  name: string;
  owner: string;
  private: boolean;
  description?: string | null;
  default_branch?: string | null;
  language?: string | null;
  stars: number;
  forks: number;
  open_issues: number;
  size_kb: number;
  pushed_at?: string | null;
  clone_url?: string | null;
  html_url?: string | null;
  archived: boolean;
  permissions?: { admin: boolean; push: boolean; pull: boolean };
}

export interface GitHubBranch {
  name: string;
  protected: boolean;
  sha: string;
}

export interface GitHubPull {
  number: number;
  title: string;
  state: string;
  draft: boolean;
  author?: string | null;
  author_avatar?: string | null;
  head?: string | null;
  base?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  merged: boolean;
  html_url?: string | null;
  additions?: number | null;
  deletions?: number | null;
  changed_files?: number | null;
  labels: string[];
  body: string;
  mergeable?: boolean | null;
  mergeable_state?: string | null;
  comments?: number;
  commits?: number;
  files?: {
    filename: string;
    status: string;
    additions: number;
    deletions: number;
    patch?: string | null;
  }[];
  checks?: {
    name?: string | null;
    status: string;
    conclusion: string | null;
    url?: string | null;
  }[];
}

export interface LocalModel {
  name: string;
  size_bytes?: number | null;
  size_gb?: number | null;
  parameter_size?: string | null;
  family?: string | null;
  quantization?: string | null;
  modified_at?: string | null;
}

export interface ModelStatus {
  provider: "ollama" | "openai" | "deepseek" | "openrouter";
  provider_label: string;
  provider_model: string;
  provider_source: "app" | "env";
  provider_key_configured: boolean;
  provider_key_source: "app" | "env" | null;
  local_fallback_enabled: boolean;
  local_fallback_available: boolean;
  free: boolean;
  base_url: string;
  available: boolean;
  reachable: boolean;
  kind: "not_running" | "no_models" | "model_missing" | "review_model_missing" | "provider_key_missing" | null;
  hint: string | null;
  chat_model: string;
  review_model: string;
  summary_mixture_models: string[];
  summary_mixture_active: boolean;
  summary_mixture_missing_models: string[];
  summary_mixture_source: "app" | "env";
  embedding_model: string;
  embedding_local: boolean;
  models: LocalModel[];
  suggested: { name: string; why: string; size_gb: number }[];
  selection_source: "app" | "env";
}
