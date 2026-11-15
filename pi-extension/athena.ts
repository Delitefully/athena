/**
 * athena's extension for a Pi worker, loaded per launch with `pi -e <this file>` (never installed into ~/.pi).
 *
 * It does for Pi what Claude Code's hooks do for a Claude worker, through the same `athena hook` command and the same events,
 * so the worker's state, its report gate and the git rules are athena's one implementation:
 *
 *   session_start        -> SessionStart       (and it writes run/<name>.pi.json: Pi's process title hides its flags)
 *   before_agent_start   -> UserPromptSubmit
 *   agent_before_settle  -> Stop               a `block` from the gate becomes one more turn, with the gate's reason
 *   agent_settled        -> Notification idle  only for a run that ended with no Stop (aborted, or failed)
 *   tool_call (bash)     -> PreToolUse         a deny blocks the command, as rules.py does for Claude
 *   session_shutdown     -> SessionEnd         when Pi quits
 *
 * It also reads the worker's inbox, run/inbox/<name>/, once a second: each message goes to Pi as the human's own words (a
 * follow-up while it works), and a quit ends Pi once it is idle. That is how HQ's `athena send` reaches a Pi worker.
 *
 * Pi reports its own working, idle, done and blocked state to Rex (OSC 7501), so nothing here draws or reports status.
 * Outside an athena worker (no ATHENA_WORKER, ATHENA_STATE_DIR and ATHENA_BIN) it does nothing.
 */
import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, readdirSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const NAME = (process.env.ATHENA_WORKER || "").trim();
const STATE = process.env.ATHENA_STATE_DIR || "";
const BIN = process.env.ATHENA_BIN || "";
const HOOK_TIMEOUT_MS = 15000;
const INBOX_EVERY_MS = 1000;
const MESSAGE_FILE = /^\d{20}-\d+\.json$/;

type Said = Record<string, any> | null;

let chain: Promise<unknown> = Promise.resolve();

/** One `athena hook` run, the event on its stdin and its JSON answer back. In order, one at a time, as Claude runs its hooks. */
function hook(event: Record<string, unknown>): Promise<Said> {
	const run = () =>
		new Promise<Said>((resolve) => {
			let out = "";
			let child;
			try {
				child = spawn(BIN, ["hook"], { env: process.env, stdio: ["pipe", "pipe", "ignore"] });
			} catch {
				resolve(null);
				return;
			}
			const timer = setTimeout(() => child.kill("SIGKILL"), HOOK_TIMEOUT_MS);
			child.stdout.on("data", (data: Buffer) => (out += data.toString()));
			child.on("error", () => {
				clearTimeout(timer);
				resolve(null);
			});
			child.on("close", () => {
				clearTimeout(timer);
				try {
					resolve(out.trim() ? JSON.parse(out) : null);
				} catch {
					resolve(null);
				}
			});
			child.stdin.on("error", () => {});
			child.stdin.end(JSON.stringify(event));
		});
	const next = chain.then(run, run);
	chain = next;
	return next;
}

function textOf(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.filter((part) => part && part.type === "text" && typeof part.text === "string")
		.map((part) => part.text)
		.join("\n");
}

/** What this run last said: the newest assistant message on the session's branch. */
function lastWords(ctx: any): string {
	try {
		const branch = ctx.sessionManager.getBranch() as any[];
		for (let at = branch.length - 1; at >= 0; at--) {
			const entry = branch[at];
			if (entry?.type === "message" && entry.message?.role === "assistant") return textOf(entry.message.content);
		}
	} catch {}
	return "";
}

function writeMark(ctx: any) {
	try {
		const run = join(STATE, "run");
		mkdirSync(run, { recursive: true });
		const target = join(run, `${NAME}.pi.json`);
		const temp = `${target}.tmp${process.pid}`;
		const session = ctx.sessionManager?.getSessionId?.() ?? null;
		writeFileSync(temp, JSON.stringify({ pid: process.pid, session, ts: Date.now() / 1000 }) + "\n", { mode: 0o600 });
		renameSync(temp, target);
	} catch {}
}

export default function (pi: ExtensionAPI) {
	if (!NAME || !STATE || !BIN) return;
	let inbox: ReturnType<typeof setInterval> | undefined;
	let stopSent = false;

	const readInbox = (ctx: any) => {
		const folder = join(STATE, "run", "inbox", NAME);
		let files: string[];
		try {
			files = readdirSync(folder).filter((f) => MESSAGE_FILE.test(f)).sort();
		} catch {
			return;
		}
		for (const file of files) {
			const taken = join(folder, `${file}.taken`);
			let message: any;
			try {
				renameSync(join(folder, file), taken); // never delivered twice, whoever else looks
				message = JSON.parse(readFileSync(taken, "utf8"));
				unlinkSync(taken);
			} catch {
				continue;
			}
			if (message?.kind === "quit") {
				ctx.shutdown();
			} else if (message?.kind === "message" && typeof message.text === "string" && message.text.trim()) {
				pi.sendUserMessage(message.text, ctx.isIdle() ? undefined : { deliverAs: "followUp" });
			}
		}
	};

	pi.on("session_start", async (event: any, ctx: any) => {
		writeMark(ctx);
		let resumed = false;
		try {
			resumed = event.reason === "resume" || (event.reason === "startup" && ctx.sessionManager.getEntries().length > 0);
		} catch {}
		await hook({ hook_event_name: "SessionStart", source: resumed ? "resume" : event.reason === "new" ? "clear" : "startup", cwd: ctx.cwd });
		if (inbox) clearInterval(inbox);
		inbox = setInterval(() => readInbox(ctx), INBOX_EVERY_MS);
	});

	pi.on("before_agent_start", async (event: any, ctx: any) => {
		await hook({ hook_event_name: "UserPromptSubmit", prompt: event.prompt ?? "", cwd: ctx.cwd });
	});

	pi.on("agent_start", async () => {
		stopSent = false;
	});

	pi.on("agent_before_settle", async (event: any, ctx: any) => {
		stopSent = true;
		const said = await hook({ hook_event_name: "Stop", last_assistant_message: lastWords(ctx), cwd: ctx.cwd });
		if (said?.decision !== "block") return;
		return {
			entries: [
				...event.entries,
				{ type: "custom_message", customType: "athena-gate", content: `athena: ${said.reason || "file your report"}`, display: true },
			],
			continue: true,
		};
	});

	pi.on("agent_settled", async (_event: any, ctx: any) => {
		if (!stopSent) await hook({ hook_event_name: "Notification", notification_type: "idle_prompt", cwd: ctx.cwd });
	});

	pi.on("tool_call", async (event: any, ctx: any) => {
		if (event.toolName !== "bash" || typeof event.input?.command !== "string") return;
		const said = await hook({ hook_event_name: "PreToolUse", tool_name: "Bash", tool_input: { command: event.input.command }, cwd: ctx.cwd });
		const out = said?.hookSpecificOutput;
		if (out?.permissionDecision === "deny") return { block: true, reason: out.permissionDecisionReason || "athena rule" };
	});

	pi.on("session_shutdown", async (event: any, ctx: any) => {
		if (inbox) clearInterval(inbox);
		inbox = undefined;
		if (event.reason === "quit") await hook({ hook_event_name: "SessionEnd", reason: "quit", cwd: ctx.cwd });
	});
}
