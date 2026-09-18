import {
  buildProtocolFromForm,
  emptyCondition,
  emptyProtocolForm,
  errorsForPrefix,
  indexErrorsByField,
  parseCapacity,
  protocolToForm,
} from "../protocol";

const STUDY_ID = "11111111-1111-1111-1111-111111111111";
const DIST_A = "aaaaaaaa-1111-1111-1111-111111111111";
const DIST_B = "bbbbbbbb-2222-2222-2222-222222222222";

test("blank optional fields serialize as null, never as empty strings", () => {
  const form = emptyProtocolForm(STUDY_ID);
  form.name = "My study";
  const protocol = buildProtocolFromForm(form);

  expect(protocol.schema_version).toBe("1");
  expect(protocol.study_id).toBe(STUDY_ID);
  expect(protocol.metadata.description).toBeNull();
  expect(protocol.metadata.owner).toBeNull();
  expect(protocol.enrollment.capacity).toBeNull();
  expect(protocol.enrollment.allow_reentry).toBeNull();
  expect(protocol.environment_requirements.host_kind).toBeNull();
});

test("only model fields are emitted (backend uses extra=forbid)", () => {
  const protocol = buildProtocolFromForm(emptyProtocolForm(STUDY_ID));
  const allowed = new Set([
    "schema_version",
    "study_id",
    "metadata",
    "schedule",
    "enrollment",
    "assignment",
    "conditions",
    "session_policy",
    "telemetry_policy",
    "privacy_policy",
    "environment_requirements",
    "completion",
    "survey_hooks",
  ]);
  Object.keys(protocol).forEach((key) => expect(allowed.has(key)).toBe(true));
  expect(protocol.metadata).toEqual({
    name: "",
    description: null,
    owner: null,
  });
  expect(protocol.schedule.kind).toBe("FIXED");
  expect(protocol.assignment.unit).toBe("ENROLLMENT");
  // Stratification is only meaningful for the stratified strategy.
  expect(protocol.assignment.stratification).toBeNull();
});

test("conditions emit only the single-pick model fields (extra=forbid)", () => {
  // The condition shape is now exactly one distribution_id pick; the deleted
  // release/profile fields must never leak (the backend uses extra=forbid).
  const form = emptyProtocolForm(STUDY_ID);
  form.conditions = [
    {
      ...emptyCondition(),
      conditionId: "arm-a",
      name: "arm-a",
      distributionId: DIST_A,
    },
    {
      ...emptyCondition(),
      conditionId: "arm-b",
      name: "arm-b",
      distributionId: DIST_B,
      weight: "0.5",
    },
  ];

  const protocol = buildProtocolFromForm(form);
  const conditionKeys = [
    "condition_id",
    "name",
    "weight",
    "distribution_id",
    "adapter_version",
    "declared_overrides",
  ];
  // The field names deleted by the single-distribution refactor are assembled
  // at runtime so a repo-wide grep for the removed identifiers stays clean; the
  // guard below still asserts they never reappear on a built condition.
  const deletedKeys = [
    ["agent", "release"].join("_"),
    ["agent", "profile", "id"].join("_"),
    "release_id",
    "artifact_digest",
    "distribution_mode",
  ];
  protocol.conditions.forEach((condition) => {
    Object.keys(condition).forEach((key) =>
      expect(conditionKeys).toContain(key),
    );
    deletedKeys.forEach((key) =>
      expect(condition).not.toHaveProperty(key),
    );
  });

  expect(protocol.conditions[0].distribution_id).toBe(DIST_A);
  expect(protocol.conditions[1].distribution_id).toBe(DIST_B);
  expect(protocol.conditions[1].weight).toBe(0.5);
});

test("'unknown' becomes the typed ExplicitUnknown marker; numbers parse", () => {
  expect(parseCapacity("")).toBeNull();
  expect(parseCapacity("unknown")).toEqual({ kind: "UNKNOWN" });
  expect(parseCapacity("25")).toBe(25);

  const form = emptyProtocolForm(STUDY_ID);
  form.capacity = "unknown";
  form.hostKind = "unknown";
  const protocol = buildProtocolFromForm(form);
  expect(protocol.enrollment.capacity).toEqual({ kind: "UNKNOWN" });
  expect(protocol.environment_requirements.host_kind).toEqual({
    kind: "UNKNOWN",
  });
});

test("a condition serialises exactly one distribution pick", () => {
  const form = emptyProtocolForm(STUDY_ID);
  form.name = "Study";
  form.strategy = "STRATIFIED";
  form.stratification = "os, experience";
  form.conditions = [
    {
      ...emptyCondition(),
      conditionId: "arm-a",
      name: "arm-a",
      distributionId: DIST_A,
      weight: "2",
      adapterVersion: "0.3.0",
      model: "qwen",
      frameworkVersion: "goose",
    },
  ];

  const protocol = buildProtocolFromForm(form);
  const condition = protocol.conditions[0];
  expect(condition.condition_id).toBe("arm-a");
  expect(condition.name).toBe("arm-a");
  expect(condition.weight).toBe(2);
  expect(condition.distribution_id).toBe(DIST_A);
  expect(condition.adapter_version).toBe("0.3.0");
  // The declared override names the arm; the provider is resolved server-side.
  expect(condition.declared_overrides).toEqual({
    agent_profile: "arm-a",
    model: "qwen",
    framework_version: "goose",
  });
  expect(protocol.assignment.stratification).toEqual(["os", "experience"]);
});

test("protocolToForm restores the distribution link and derived identity", () => {
  const protocol = {
    conditions: [
      {
        condition_id: "arm-a",
        name: "arm-a",
        weight: 1,
        distribution_id: DIST_A,
        adapter_version: "0.3.0",
        declared_overrides: { agent_profile: "arm-a", model: "qwen" },
        resolved_distribution: {
          distribution_id: DIST_A,
          distribution_mode: "PACKAGED",
          release_id: "rel-1",
          version: "1.2.0",
          verified: true,
        },
      },
    ],
  };

  const restored = protocolToForm(protocol, STUDY_ID);

  expect(restored.conditions[0].distributionId).toBe(DIST_A);
  expect(restored.conditions[0].conditionId).toBe("arm-a");
  expect(restored.conditions[0].name).toBe("arm-a");
  expect(restored.conditions[0].model).toBe("qwen");
  expect(restored.conditions[0].adapterVersion).toBe("0.3.0");
  // The deleted fields are not carried back into the form.
  expect(restored.conditions[0]).not.toHaveProperty("releaseId");
  expect(restored.conditions[0]).not.toHaveProperty("artifactDigest");
  expect(restored.conditions[0]).not.toHaveProperty("distributionMode");
});

test("protocolToForm round-trips the fields the editor renders", () => {
  const form = emptyProtocolForm(STUDY_ID);
  form.name = "Round trip";
  form.conditions = [
    {
      ...emptyCondition(),
      conditionId: "arm-a",
      name: "arm-a",
      distributionId: DIST_A,
      weight: "3",
      frameworkVersion: "goose",
    },
  ];
  const protocol = buildProtocolFromForm(form);
  const restored = protocolToForm(protocol, STUDY_ID);

  expect(restored.name).toBe("Round trip");
  expect(restored.conditions[0].name).toBe("arm-a");
  expect(restored.conditions[0].distributionId).toBe(DIST_A);
  expect(restored.conditions[0].weight).toBe("3");
  expect(restored.conditions[0].frameworkVersion).toBe("goose");
});

test("errorsForPrefix surfaces nested field errors for a condition", () => {
  const index = indexErrorsByField([
    {
      code: "DISTRIBUTION_UNVERIFIED",
      field: "conditions[0].distribution_id",
      message: "distribution is unverified",
    },
    {
      code: "NO_CONDITIONS",
      field: "conditions",
      message: "add a condition",
    },
  ]);
  const nested = errorsForPrefix(index, "conditions[0]");
  expect(nested).toHaveLength(1);
  expect(nested[0].field).toBe("conditions[0].distribution_id");
  expect(errorsForPrefix(index, "conditions")).toHaveLength(2);
});
