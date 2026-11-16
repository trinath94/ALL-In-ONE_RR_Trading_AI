"use client";
import { ResearchSummaryPanel } from "@/components/AiText";
import { ResearchLab } from "@/components/Intel";
import { PageTitle } from "@/components/Shell";

/** Part 17 item 20: strategy research and development in the isolated experimental sandbox (proposals, development,
 *  unit tests, validation, DEMO forward tests), with the AI research digest. Production strategies are never changed
 *  here; promotion needs evidence and administrator approval. */
export default function StrategyResearchLab() {
  return (
    <div>
      <PageTitle sub="New strategy ideas are developed and tested in the isolated experimental sandbox. The 215 production strategies are never modified here; promotion requires evidence and administrator approval.">
        Strategy Research Lab</PageTitle>
      <div className="space-y-3"><ResearchLab /><ResearchSummaryPanel /></div>
    </div>
  );
}
