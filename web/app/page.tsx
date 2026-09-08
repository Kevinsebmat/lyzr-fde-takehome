"use client";

import { useEffect, useState } from "react";
import { get, type Health } from "@/lib/api";
import { P01, P02, P03, P04 } from "@/components/panels-a";
import { P05, P06, P07 } from "@/components/panels-b";
import { P08, P09, P10 } from "@/components/panels-c";
import { P11 } from "@/components/panels-d";
import { Tag } from "@/components/ui";

/** Each project is named by the failure it survives — that is the organising
 *  idea of the repo, so it is the organising idea of the console too. */
const PROJECTS = [
  { slug: "p01", name: "Structured output", failure: "output you cannot parse, or must not trust", panel: P01 },
  { slug: "p02", name: "RAG with citations", failure: "a confident answer the sources do not support", panel: P02 },
  { slug: "p03", name: "ReAct planner", failure: "the agent that never stops", panel: P03 },
  { slug: "p04", name: "Tool orchestrator", failure: "an unauthorised call, and sources that disagree", panel: P04 },
  { slug: "p05", name: "Memory", failure: "amnesia — and confidently remembering what changed", panel: P05 },
  { slug: "p06", name: "Approval gate", failure: "an irreversible action nobody authorised", panel: P06 },
  { slug: "p07", name: "Cost router", failure: "a cascade everyone believes is saving money", panel: P07 },
  { slug: "p08", name: "Event automation", failure: "the same refund issued twice", panel: P08 },
  { slug: "p09", name: "Debate", failure: "four agents agreeing because they were primed to", panel: P09 },
  { slug: "p10", name: "Self-reflection", failure: "shipping a worse draft than you started with", panel: P10 },
  { slug: "p11", name: "Observability", failure: "misbehaviour every dashboard calls healthy", panel: P11 },
] as const;

export default function Console() {
  const [active, setActive] = useState(0);
  const [health, setHealth] = useState<Health | null>(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setHealth(await get<Health>("/api/health"));
      } catch {
        setOffline(true);
      }
    })();
  }, []);

  const project = PROJECTS[active];
  const Panel = project.panel;

  return (
    <div className="shell">
      <header className="head">
        <span className="head-title">Agent console</span>
        <span className="head-sub">eleven projects · one shared core</span>
        <span className="head-spacer" />
        {offline ? (
          <Tag tone="fail">API unreachable — start it with make dev</Tag>
        ) : health ? (
          <Tag tone={health.live ? "ok" : "info"}>
            {health.live ? "live — real model calls" : "offline — recorded responses"}
          </Tag>
        ) : null}
      </header>

      <nav className="rail" aria-label="Projects">
        <div className="rail-heading">Failure modes</div>
        {PROJECTS.map((p, i) => (
          <button
            key={p.slug}
            className="rail-item"
            aria-current={i === active}
            onClick={() => setActive(i)}
          >
            <span className="rail-slug">{p.slug}</span>
            <span className="rail-name">{p.name}</span>
          </button>
        ))}
      </nav>

      <main className="main">
        <div className="main-inner">
          <div className="panel-head">
            <h1 className="panel-title">
              <span style={{ color: "var(--muted)", marginRight: 10 }}>{project.slug}</span>
              {project.name}
            </h1>
            <div className="failure-mode">
              <span className="failure-mode-label">Survives</span>
              {project.failure}
            </div>
          </div>
          <Panel key={project.slug} />
        </div>
      </main>
    </div>
  );
}
