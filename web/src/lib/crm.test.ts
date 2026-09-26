import { describe, expect, it } from "vitest";
import { editableLead, serializeLeadInput, type Lead } from "./crm";

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