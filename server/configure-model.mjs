// Idempotently declare jev/auto in the embedded router's protected user-model
// overlay. Existing user-curated routes are preserved.
import {
  readUserModels,
  writeUserModels,
} from "../router/src/user-models.mjs";

export const JEV_MODEL = Object.freeze({
  slug: "jev/auto",
  gatewayModel: "jev-auto",
  compHash: "jev-auto-user-v1",
  upstreamModel: "auto",
  provider: "jev",
  listed: true,
  displayName: "Jev Codex Router",
  description:
    "Jev automatically selects both model and reasoning depth for each task phase; provider fallbacks follow the configured ladder.",
  priority: 95,
  // Internal compatibility metadata for the registry and bypass modes. The
  // published jev/auto catalog omits manual effort controls; normal routing
  // uses the depth chosen by Jev, regardless of the client's saved setting.
  defaultEffort: "medium",
  reasoningLevels: [
    { effort: "low", description: "Quick reasoning" },
    { effort: "medium", description: "Balanced reasoning" },
    { effort: "high", description: "Deep reasoning" },
    { effort: "xhigh", description: "Extended reasoning" },
    { effort: "max", description: "Maximum reasoning" },
  ],
  contextWindow: 258400,
  autoCompact: 219640,
  searchTool: { mode: "hosted" },
  supportsSearchHistory: true,
  inputModalities: ["text", "image"],
});

const existing = readUserModels();
const next = [
  ...existing.filter((model) => model?.slug !== JEV_MODEL.slug),
  JEV_MODEL,
];
writeUserModels(next);
process.stdout.write(`${JSON.stringify({
  configured: JEV_MODEL.slug,
  preservedModels: existing.filter((model) => model?.slug !== JEV_MODEL.slug).length,
})}\n`);
