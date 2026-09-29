import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  FileCode,
  Folder,
  FolderOpen,
} from "lucide-react";
import type { ReviewSection } from "../../hooks/useMultiReview";
import { reviewPath } from "../../lib/reviewFindings";

export interface ReviewFolder {
  key: string;
  name: string;
  folders: ReviewFolder[];
  files: ReviewSection[];
}
export function buildReviewTree(files: ReviewSection[]): ReviewFolder {
  const root: ReviewFolder = { key: "", name: "", folders: [], files: [] };
  for (const file of files) {
    const [repo] = file.id.split("::");
    const segments = reviewPath(file.id)
      .split("/")
      .filter(Boolean)
      .slice(0, -1);
    if (file.id.includes("::")) segments.unshift(repo);
    let node = root;
    const path: string[] = [];
    for (const part of segments) {
      path.push(part);
      const key = JSON.stringify(path);
      let folder = node.folders.find((f) => f.key === key);
      if (!folder) {
        folder = {
          key,
          name: part.startsWith("https://")
            ? part.replace(/^https:\/\/[^/]+\//, "")
            : part,
          folders: [],
          files: [],
        };
        node.folders.push(folder);
      }
      node = folder;
    }
    node.files.push(file);
  }
  const sort = (node: ReviewFolder) => {
    node.folders.sort((a, b) => a.name.localeCompare(b.name));
    node.files.sort((a, b) => a.fileName.localeCompare(b.fileName));
    node.folders.forEach(sort);
  };
  sort(root);
  return root;
}
export function ReviewFileTree({
  files,
  selected,
  onSelect,
  outcome,
}: {
  files: ReviewSection[];
  selected: string;
  onSelect: (id: string) => void;
  outcome: (file: ReviewSection) => string;
}) {
  const tree = useMemo(() => buildReviewTree(files), [files]);
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const selectedRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const ancestors: string[] = [];
    const walk = (node: ReviewFolder): boolean => {
      const found =
        node.files.some((f) => f.id === selected) || node.folders.some(walk);
      if (found) ancestors.push(node.key);
      return found;
    };
    walk(tree);
    setClosed(
      (old) => new Set([...old].filter((key) => !ancestors.includes(key))),
    );
    // Only reveal the active item inside this sidebar, never scroll the code pane.
    const el = selectedRef.current;
    if (el) {
      const container = el.closest("[data-file-tree-scroll]");
      if (container) {
        const a = el.getBoundingClientRect(),
          b = container.getBoundingClientRect();
        if (a.top < b.top || a.bottom > b.bottom)
          container.scrollTop += a.top - b.top - b.height / 2;
      }
    }
    // Streaming content must not reopen a folder the reader just collapsed.
  }, [selected]);
  const renderFolder = (node: ReviewFolder, depth: number) => (
    <div key={node.key}>
      {node.name && (
        <button
          type="button"
          aria-expanded={!closed.has(node.key)}
          aria-label={`Folder ${node.name}`}
          className="sf-review-folder"
          style={{ paddingLeft: depth * 12 + 8 }}
          onClick={() =>
            setClosed((old) => {
              const next = new Set(old);
              next.has(node.key) ? next.delete(node.key) : next.add(node.key);
              return next;
            })
          }
        >
          {closed.has(node.key) ? (
            <ChevronRight size={12} />
          ) : (
            <ChevronDown size={12} />
          )}
          {closed.has(node.key) ? (
            <Folder size={14} />
          ) : (
            <FolderOpen size={14} />
          )}
          <span className="truncate">{node.name}</span>
        </button>
      )}
      {!closed.has(node.key) && (
        <>
          {node.folders.map((folder) =>
            renderFolder(folder, node.name ? depth + 1 : depth),
          )}
          {node.files.map((file) => (
            <button
              type="button"
              key={file.id}
              ref={selected === file.id ? selectedRef : undefined}
              aria-current={selected === file.id ? "true" : undefined}
              aria-label={`${reviewPath(file.id)}: ${outcome(file)}`}
              title={`${file.id}\n${outcome(file)}`}
              className={`sf-review-tree-file ${selected === file.id ? "is-current" : ""}`}
              style={{ paddingLeft: (node.name ? depth + 1 : depth) * 12 + 12 }}
              onClick={() => onSelect(file.id)}
            >
              <FileCode size={13} className="shrink-0" />
              <span className="truncate">
                {reviewPath(file.id).split("/").pop()}
              </span>
              <span className="sf-accent ml-auto text-[10px]">
                {file.findings?.length || ""}
              </span>
              <span
                aria-hidden
                className={`sf-review-file-dot ${file.fallbackReason || file.status === "error" ? "is-warning" : file.status === "complete" ? "is-done" : ""}`}
              />
            </button>
          ))}
        </>
      )}
    </div>
  );
  return <nav aria-label="Review file tree">{renderFolder(tree, 0)}</nav>;
}
