import resolution from "../../../../../../tests/fixtures/research/telemetry_policy_resolution.json";
import { collectedClasses, resolveFieldClasses } from "../studyUtils";

// The same table the server test checks against ingestion
// (tests/backend_tests/research/test_privacy_policy_vocabulary.py).
test.each(resolution.cases.map((item) => [JSON.stringify(item.declared), item.declared, item.runtime]))(
  "%s resolves like ingestion",
  (_label, declared, runtime) => {
    expect([...resolveFieldClasses(declared)].sort()).toEqual(runtime);
  },
);

test("event records and code metadata are always stored; content only with content_capture", () => {
  expect(collectedClasses({ allowed_field_classes: ["CONTENT"], content_capture: false })).toEqual([
    "EVENTS",
    "CODE_METADATA_HASHED",
  ]);
  expect(collectedClasses({ allowed_field_classes: ["METRICS"], content_capture: true })).toEqual([
    "EVENTS",
    "SYSTEM",
    "CODE_METADATA_HASHED",
    "CONTENT",
  ]);
  expect(collectedClasses({})).toEqual(["EVENTS", "BEHAVIORAL", "SYSTEM", "CODE_METADATA"]);
});

test("only strings are class names, as on the server", () => {
  expect(resolveFieldClasses([["METRICS"], 7, null])).toEqual(["BEHAVIORAL", "SYSTEM", "CODE_METADATA"]);
});
