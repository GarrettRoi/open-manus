import { describe, expect, it } from "vitest";
import { crmReadState, crmScope, editableLead, leadUrgency, serializeLeadInput, type Lead, type ListArgs } from "./crm";

const stored: Lead = {
  id: "lead-18", revision: 7, created_at: "2026-01-01", updated_at: "2026-01-02",
  archived: false, duplicate_ids: ["lead-12"], notes_total: 1, history_total: 1,
  notes: [{ text: "Called", actor: "agent", created_at: 1767225600 }],
  history: [{ action: "update", actor: "agent", at: 1767225600 }],
  name: "Avery Cole", email: "avery@example.org", phone: "4055550123",
  company: "Cole Studio", source_id: "vowsok", external_id: "form-57",
  business: "dj_wedding", lead_type: "Wedding", status: "qualified",
  assigned_agent: "samantha", estimated_value: "1750.00", currency: "USD",
  acquisition_date: "2026-01-01", action_timeframe: "This month",
  next_action_date: "2026-01-08", wedding_date: "2026-05-09",
  custom_fields: { guests: 125, confirmed: true },
};

describe("CRM edit serialization", () => {
  it("picks every editable lead field but never includes record metadata in changes", () => {
    const form = editableLead(stored);
    const changes = serializeLeadInput(form, "update");
    expect(changes).toMatchObject({
      name: "Avery Cole", email: "avery@example.org", phone: "4055550123",
      company: "Cole Studio", source_id: "vowsok", external_id: "form-57",
      business: "dj_wedding", lead_type: "Wedding", status: "qualified",
      assigned_agent: "samantha", estimated_value: "1750.00", currency: "USD",
      acquisition_date: "2026-01-01", action_timeframe: "This month",
      next_action_date: "2026-01-08", wedding_date: "2026-05-09",
      custom_fields: { guests: 125, confirmed: true },
    });
    expect(Object.keys(changes).sort()).toEqual([
      "name", "email", "phone", "company", "source_id", "external_id",
      "business", "lead_type", "status", "assigned_agent", "estimated_value",
      "currency", "acquisition_date", "action_timeframe", "next_action_date",
      "wedding_date", "custom_fields",
    ].sort());
    expect(JSON.stringify({ id: stored.id, revision: stored.revision, changes })).not.toContain('"history"');
    expect(JSON.stringify(changes)).not.toContain('"revision"');
  });

  it("replaces custom values without Not set booleans and explicitly clears edited money", () => {
    const form = editableLead(stored);
    form.estimated_value = "";
    form.custom_fields = { guests: 125, confirmed: null };
    expect(serializeLeadInput(form, "update", stored.estimated_value)).toMatchObject({
      estimated_value: null,
      custom_fields: { guests: 125 },
    });
    expect(serializeLeadInput(form, "create")).not.toHaveProperty("estimated_value");
    expect(serializeLeadInput(form, "update", stored.estimated_value).custom_fields).not.toHaveProperty("confirmed");
    expect(serializeLeadInput(form, "update")).not.toHaveProperty("estimated_value");
  });
});

describe("CRM pipeline and activity scope", () => {
  const filters: ListArgs = {
    query: "4055550123", business: "dj_wedding", status: "qualified",
    source_id: "vowsok", assigned_agent: "samantha", archived: false,
    urgency: "overdue", sort: "next_action", page: 4, limit: 20,
  };
  it("preserves every selected business/contact filter without queue, sort or pagination", () => {
    expect(crmScope(filters)).toEqual({
      query: "4055550123", business: "dj_wedding", status: "qualified",
      source_id: "vowsok", assigned_agent: "samantha", archived: false,
    });
    expect({ ...crmScope(filters), page: 2, limit: 20 }).not.toHaveProperty("urgency");
    expect(crmScope({ ...filters, business: "real_estate" }).business).toBe("real_estate");
  });
  it("omits empty selectors and keeps the explicit archive flag", () => {
    expect(crmScope({ page: 1, limit: 20, query: "", business: "", status: "", archived: false })).toEqual({ archived: false });
    expect(crmScope({ page: 1, limit: 20, archived: true })).toEqual({ archived: true });
  });
  it.each(["Avery Cole", "avery@example.org", "+1 405 555 0123"])("does not alter the name/email/phone search %s", query => {
    expect(crmScope({ ...filters, query }).query).toBe(query);
  });
});

describe("CRM server calendar urgency", () => {
  it("distinguishes overdue, due today and future action dates using server today", () => {
    expect(leadUrgency(stored, "2026-01-09")).toBe("overdue");
    expect(leadUrgency(stored, "2026-01-08")).toBe("today");
    expect(leadUrgency(stored, "2026-01-07")).toBeNull();
  });
  it.each(["won", "lost"] as const)("excludes %s leads even when the date is overdue", status => {
    expect(leadUrgency({ ...stored, status }, "2026-01-09")).toBeNull();
  });
  it("excludes archived, undated and calendar-not-yet-loaded leads", () => {
    expect(leadUrgency({ ...stored, archived: true }, "2026-01-09")).toBeNull();
    expect(leadUrgency({ ...stored, next_action_date: "" }, "2026-01-09")).toBeNull();
    expect(leadUrgency({ ...stored, next_action_date: undefined }, "2026-01-09")).toBeNull();
    expect(leadUrgency(stored, "")).toBeNull();
  });
});

describe("CRM request-keyed read states", () => {
  const old = { key: "dj:page1:revision1", data: { total: 18 }, error: "" };
  it("only displays the exact current request's snapshot", () => {
    expect(crmReadState(old, old.key)).toEqual({ data: { total: 18 }, error: "", loading: false });
  });
  it.each(["real_estate:page1:revision1", "dj:page2:revision1", "dj:page1:revision2"])("hides stale results before effects run for %s", key => {
    expect(crmReadState(old, key)).toEqual({ data: null, error: "", loading: true });
  });
  it("never presents stale counts or rows alongside a request failure", () => {
    expect(crmReadState({ ...old, error: "Permission denied" }, old.key)).toEqual({ data: null, error: "Permission denied", loading: false });
  });
  it("shows a skeleton until a request settles, including after a retry", () => {
    expect(crmReadState(null, old.key)).toEqual({ data: null, error: "", loading: true });
    expect(crmReadState({ ...old, error: "Unavailable" }, "retry")).toEqual({ data: null, error: "", loading: true });
  });
  it("does not render hidden-tab results, and treats a valid empty result as settled", () => {
    expect(crmReadState(old, old.key, false)).toEqual({ data: null, error: "", loading: false });
    expect(crmReadState({ ...old, data: { total: 0 } }, old.key)).toEqual({ data: { total: 0 }, error: "", loading: false });
  });
});