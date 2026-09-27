import { jsxs as t, jsx as n, Fragment as u } from "react/jsx-runtime";
import { useAppApi as L } from "@kirocrew/app-sdk";
import { PageHeader as k } from "@kirocrew/app-sdk/ui";
import { useRef as N, useState as m, useEffect as R } from "react";
const p = {
  background: "var(--card)",
  color: "var(--card-fg)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: "1rem"
}, d = {
  color: "var(--muted)"
};
function g(e) {
  const r = e === "warn" ? "--warn-subtle" : e === "danger" ? "--danger-subtle" : "--ok-subtle", i = e === "warn" ? "--warn" : e === "danger" ? "--danger" : "--ok";
  return {
    background: `var(${r})`,
    color: `var(${i})`,
    border: `1px solid var(${i})`,
    borderRadius: 6,
    padding: "0.5rem 0.75rem",
    marginTop: "0.5rem"
  };
}
function z() {
  return {
    background: "var(--accent-subtle)",
    color: "var(--accent)",
    border: "1px solid var(--border)",
    borderRadius: 999,
    padding: "0.25rem 0.75rem",
    display: "inline-flex",
    alignItems: "center",
    gap: "0.375rem",
    fontSize: "0.85rem"
  };
}
function w() {
  return {
    background: "var(--accent)",
    color: "var(--card)",
    border: "1px solid var(--border)",
    borderRadius: 6,
    padding: "0.4rem 0.9rem",
    cursor: "pointer",
    fontSize: "0.85rem"
  };
}
function v(e) {
  if (!e) return "never";
  try {
    return new Date(e).toLocaleString();
  } catch {
    return e;
  }
}
function $({
  drift: e,
  onPushNow: r,
  pushing: i
}) {
  return /* @__PURE__ */ t("div", { style: p, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "Local changes" }),
    /* @__PURE__ */ n("p", { style: { fontSize: "1.5rem", margin: "0.5rem 0" }, children: e ? "Yes" : "No changes yet" }),
    e ? /* @__PURE__ */ n("button", { type: "button", style: w(), onClick: r, disabled: i, children: i ? "Pushing…" : "Push now" }) : /* @__PURE__ */ n("p", { style: d, children: "Tracked tree matches the last pushed hash." })
  ] });
}
function O({
  lastPush: e,
  failure: r
}) {
  return /* @__PURE__ */ t("div", { style: p, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "Last push" }),
    e ? /* @__PURE__ */ t(u, { children: [
      /* @__PURE__ */ n("p", { style: { margin: "0.5rem 0" }, children: v(e.time) }),
      /* @__PURE__ */ n("p", { children: /* @__PURE__ */ n("a", { href: e.pr_url, target: "_blank", rel: "noreferrer", children: e.pr_url }) }),
      /* @__PURE__ */ n("p", { style: d, children: e.merged ? "Merged" : "Open — needs merging" })
    ] }) : /* @__PURE__ */ n("p", { style: d, children: "No push yet — never pushed." }),
    r ? /* @__PURE__ */ t("p", { style: { color: "var(--danger)" }, children: [
      r.reason,
      r.time ? ` (${v(r.time)})` : null
    ] }) : null
  ] });
}
function P({
  lastSeenSha: e,
  pollFailure: r
}) {
  return /* @__PURE__ */ t("div", { style: p, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "From main" }),
    /* @__PURE__ */ n("p", { style: { fontSize: "1.1rem", margin: "0.5rem 0" }, children: "Up to date" }),
    /* @__PURE__ */ t("p", { style: d, children: [
      "Last-seen sha: ",
      e ? e.slice(0, 7) : "none"
    ] }),
    r ? /* @__PURE__ */ t("div", { style: { color: "var(--danger)" }, children: [
      /* @__PURE__ */ t("p", { style: { margin: "0.25rem 0" }, children: [
        r.reason,
        r.time ? ` (${v(r.time)})` : null
      ] }),
      /* @__PURE__ */ n("p", { style: d, children: "The poll job is failing — see the reason above. Repeated failures may pause the poll job (KiroCrew pauses a cron after 5 consecutive failures); the status response has no field reporting a consecutive-failure count or paused state." })
    ] }) : null
  ] });
}
const T = [
  { cls: "live_immediate", label: "live now" },
  { cls: "live_within_60s", label: "live within 60s" },
  { cls: "live_in_new_session", label: "live in a new session" },
  { cls: "live_on_next_resolution", label: "live on next resolution" }
];
function E({ lastApply: e }) {
  const r = {
    live_immediate: 0,
    live_within_60s: 0,
    live_in_new_session: 0,
    live_on_next_resolution: 0
  };
  for (const i of Object.values(e.propagation ?? {}))
    i.propagation_class !== void 0 && (r[i.propagation_class] = (r[i.propagation_class] ?? 0) + 1);
  return /* @__PURE__ */ n("div", { style: { display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.75rem" }, children: T.map(({ cls: i, label: c }) => /* @__PURE__ */ t("span", { style: z(), children: [
    c,
    /* @__PURE__ */ n("span", { children: r[i] })
  ] }, i)) });
}
function U({
  lastApply: e,
  onUndo: r,
  undoing: i
}) {
  var c, h;
  return /* @__PURE__ */ t("div", { style: p, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "Last apply" }),
    e ? /* @__PURE__ */ t(u, { children: [
      /* @__PURE__ */ n("p", { style: { margin: "0.5rem 0" }, children: e.outcome === "applied" ? "Applied" : `Outcome: ${e.outcome}` }),
      /* @__PURE__ */ t("p", { style: d, children: [
        "Applied sha: ",
        e.sha.slice(0, 7)
      ] }),
      /* @__PURE__ */ n(
        "button",
        {
          type: "button",
          style: w(),
          onClick: () => r(e.apply_id),
          disabled: i,
          children: i ? "Undoing…" : "Undo this apply"
        }
      ),
      /* @__PURE__ */ n(E, { lastApply: e }),
      e.paused_cron_names.length > 0 ? /* @__PURE__ */ t("div", { style: g("warn"), children: [
        /* @__PURE__ */ n("strong", { children: "Paused crons" }),
        /* @__PURE__ */ n("ul", { style: { margin: "0.25rem 0 0", paddingLeft: "1.25rem" }, children: e.paused_cron_names.map((a) => {
          const s = e.changed_commands.find((o) => o.name === a);
          return /* @__PURE__ */ t("li", { children: [
            a,
            s ? /* @__PURE__ */ t(u, { children: [
              " — ",
              /* @__PURE__ */ n("code", { children: s.command })
            ] }) : null
          ] }, a);
        }) })
      ] }) : null,
      Object.keys(e.not_applied).length > 0 ? /* @__PURE__ */ t("div", { style: g("warn"), children: [
        /* @__PURE__ */ n("strong", { children: "Not applied" }),
        /* @__PURE__ */ n("ul", { style: { margin: "0.25rem 0 0", paddingLeft: "1.25rem" }, children: Object.entries(e.not_applied).map(([a, s]) => /* @__PURE__ */ t("li", { children: [
          a,
          s ? `: ${s}` : null
        ] }, a)) })
      ] }) : null,
      (() => {
        const a = new Set(
          Object.keys(e.not_applied).map((o) => o.split(":")[0])
        ), s = e.needs_credential.filter(
          (o) => !a.has(o.split(":")[0])
        );
        return s.length > 0 ? /* @__PURE__ */ t("div", { style: g("danger"), children: [
          /* @__PURE__ */ n("strong", { children: "Needs credential" }),
          /* @__PURE__ */ n("ul", { style: { margin: "0.25rem 0 0", paddingLeft: "1.25rem" }, children: s.map((o) => /* @__PURE__ */ n("li", { children: o }, o)) })
        ] }) : null;
      })(),
      e.applied.includes("crons.json") || e.applied.includes("instances.json") || (((c = e.changed_instance_names) == null ? void 0 : c.length) ?? 0) > 0 ? /* @__PURE__ */ t("div", { style: g("ok"), children: [
        "This is a deliberate exception to instance isolation: crons.json/instances.json sync by design.",
        (((h = e.changed_instance_names) == null ? void 0 : h.length) ?? 0) > 0 ? /* @__PURE__ */ t(u, { children: [
          " ",
          "Instances affected: ",
          e.changed_instance_names.join(", "),
          "."
        ] }) : null
      ] }) : null
    ] }) : /* @__PURE__ */ n("p", { style: d, children: "Never applied yet." })
  ] });
}
function F() {
  const e = L(), r = N(e);
  r.current = e;
  const [i, c] = m(null), [h, a] = m(null), [s, o] = m(!0), [S, _] = m(!1), [x, b] = m(!1);
  async function f() {
    try {
      const l = await r.current.get("/status");
      c(l), a(null);
    } catch (l) {
      a(l instanceof Error ? l.message : String(l));
    } finally {
      o(!1);
    }
  }
  R(() => {
    f();
  }, []);
  async function C() {
    _(!0);
    try {
      await r.current.post("/push"), await f();
    } catch (l) {
      a(l instanceof Error ? l.message : String(l));
    } finally {
      _(!1);
    }
  }
  async function j(l) {
    b(!0);
    try {
      await r.current.post(`/restore/${l}`), await f();
    } catch (y) {
      a(y instanceof Error ? y.message : String(y));
    } finally {
      b(!1);
    }
  }
  return /* @__PURE__ */ t(u, { children: [
    /* @__PURE__ */ n(k, { title: "Config bundle status", subtitle: "Keeps this box aligned with Kiro-Config-Bundles" }),
    /* @__PURE__ */ n("div", { className: "px-6 pb-8 overflow-y-auto flex-1 min-h-0", children: s ? /* @__PURE__ */ n("p", { style: d, children: "Loading…" }) : h ? /* @__PURE__ */ n("div", { style: p, children: /* @__PURE__ */ n("p", { style: { color: "var(--danger)" }, children: h }) }) : i ? /* @__PURE__ */ t(u, { children: [
      /* @__PURE__ */ t(
        "div",
        {
          style: {
            display: "grid",
            gap: "0.875rem",
            gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            marginBottom: "1.5rem"
          },
          children: [
            /* @__PURE__ */ n($, { drift: i.drift, onPushNow: C, pushing: S }),
            /* @__PURE__ */ n(O, { lastPush: i.last_push, failure: i.last_push_failure }),
            /* @__PURE__ */ n(
              P,
              {
                lastSeenSha: i.last_seen_sha,
                pollFailure: i.last_poll_failure
              }
            )
          ]
        }
      ),
      /* @__PURE__ */ n(U, { lastApply: i.last_apply, onUndo: j, undoing: x })
    ] }) : null })
  ] });
}
export {
  F as default
};
