// Validate APL documents with Amazon's apl-suggester (npm install apl-suggester).
// Usage: node tools/validate-apl.js <document.json>...
const fs = require("fs");
const { StaticAplTemplateValidator } = require("apl-suggester");

(async () => {
  let failed = false;
  for (const path of process.argv.slice(2)) {
    const doc = JSON.parse(fs.readFileSync(path, "utf8"));
    const results = await new StaticAplTemplateValidator().validate(doc);
    const problems = (results || []).filter((r) => r.level !== "INFO");
    console.log(`${path}: ${problems.length ? problems.length + " problem(s)" : "ok"}`);
    for (const p of problems) console.log(`  [${p.level}] ${p.path || ""} ${p.errorMessage || JSON.stringify(p)}`);
    if (problems.some((p) => p.level === "ERROR")) failed = true;
  }
  process.exit(failed ? 1 : 0);
})();
