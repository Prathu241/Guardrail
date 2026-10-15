// Build the Round 1 Word file from ABSTRACT.md and architecture.png.
//   node docs/round1/build_docx.js
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, ImageRun, HeadingLevel, AlignmentType,
  LevelFormat, PageBreak, BorderStyle, LineRuleType,
} = require("docx");

const HERE = __dirname;
const md = fs.readFileSync(path.join(HERE, "ABSTRACT.md"), "utf8").split("\n");
const FONT = "Arial";
const BLUE = "2C4A8A";

const CAPTION =
  "The dashed blue box is the only place anything changes: everything in it happens in one database " +
  "transaction, so a check cannot pass on stale records and a crash leaves nothing half-written. Payments " +
  "and messages cannot be rolled back, so the tool records them as outbox intents and the dispatcher delivers " +
  "them after commit with the intent's id as idempotency key. The reconciler compares the providers' records " +
  "with the outbox and audit log.";

// **bold** spans become bold runs; everything else is plain text.
function runs(text, opts = {}) {
  return text.split(/(\*\*[^*]+\*\*)/).filter(Boolean).map((part) =>
    part.startsWith("**")
      ? new TextRun({ text: part.slice(2, -2), bold: true, ...opts })
      : new TextRun({ text: part, ...opts }));
}

const children = [];
for (const line of md) {
  if (!line.trim()) continue;
  if (line.startsWith("# ")) {
    children.push(new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun(line.slice(2))] }));
  } else if (line.startsWith("**Team**")) {
    children.push(new Paragraph({
      spacing: { after: 160 },
      border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "B8C2D6", space: 6 } },
      children: runs(line, { size: 18, color: "3A4458" }),
    }));
  } else if (line.startsWith("## ")) {
    children.push(new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(line.slice(3))] }));
  } else if (/^\d+\. /.test(line)) {
    children.push(new Paragraph({
      numbering: { reference: "steps", level: 0 },
      alignment: AlignmentType.JUSTIFIED,
      children: runs(line.replace(/^\d+\. /, "")),
    }));
  } else {
    children.push(new Paragraph({ alignment: AlignmentType.JUSTIFIED, children: runs(line) }));
  }
}

children.push(new Paragraph({ children: [new PageBreak()] }));
children.push(new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("Architecture")] }));
children.push(new Paragraph({
  children: [new TextRun("How one proposed tool call moves through Guardrail. Outcomes are colour-coded and " +
    "labelled: allow (green), escalate (amber), block (red).")],
}));
// A4 portrait with 16 mm margins leaves about 178 mm (≈ 672 px at 96 dpi) of width.
const png = fs.readFileSync(path.join(HERE, "architecture.png"));
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { before: 120, after: 120, line: 240, lineRule: LineRuleType.AUTO },
  children: [new ImageRun({
    type: "png", data: png,
    transformation: { width: 660, height: Math.round(660 * 1400 / 2500) },
    altText: { title: "Guardrail architecture", description: "Pipeline, approval queue, transaction and outbox",
      name: "architecture" },
  })],
}));
children.push(new Paragraph({
  alignment: AlignmentType.JUSTIFIED,
  children: [new TextRun({ text: CAPTION, size: 18, color: "3A4458" })],
}));

const doc = new Document({
  creator: "Team SaptaX",
  title: "Guardrail: Round 1 Abstract",
  styles: {
    default: { document: { run: { font: FONT, size: 20 }, paragraph: { spacing: { after: 80, line: 276, lineRule: LineRuleType.AUTO } } } },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", next: "Normal",
        run: { font: FONT, size: 32, bold: true, color: "1B2230" },
        paragraph: { spacing: { after: 80 } } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 20, bold: true, allCaps: true, color: BLUE },
        paragraph: { spacing: { before: 200, after: 60 }, outlineLevel: 0, keepNext: true } },
    ],
  },
  numbering: {
    config: [{
      reference: "steps",
      levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 360, hanging: 260 } } } }],
    }],
  },
  sections: [{
    properties: { page: { margin: { top: 907, bottom: 907, left: 907, right: 907 } } },
    children,
  }],
});

const out = path.join(HERE, "Guardrail_Round1_Abstract.docx");
Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(out, buf); console.log(`wrote ${out}`); });
