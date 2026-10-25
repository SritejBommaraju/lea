import SwiftUI

// The pieces of the report sheet that rises over the stage (DESIGN §7.5): the verdict (the top finding as the
// headline, one sentence of counts, the person's own words), the safety card with every backend note, problem cards
// laid out like specimen labels and numbered like their markers, setup cards in the setup tone, and "What wasn't
// checked" as a sheet off the default path. Words come from ReportWords (report fields only).

/// The headline, one sentence of counts, then the person's own words, the photo count and the date. No eyebrow above
/// it and no rule under it.
struct StageVerdictHeader: View {
    let report: ReportModel
    var symptom: String?

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(ReportWords.headline(report))
                .display(.hero)
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
            if let sentence = ReportWords.sentence(report).map(ReportWords.display) {
                Text(sentence).font(.body).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if !meta.isEmpty {
                Text(meta).font(.footnote).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    private var meta: String {
        var parts: [String] = []
        if let symptom = symptom?.trimmedOrNil { parts.append("\u{201C}\(symptom)\u{201D}") }
        if report.photos > 0 { parts.append(report.photos == 1 ? "1 photo" : "\(report.photos) photos") }
        if let date = report.createdAt { parts.append(date.formatted(.dateTime.month(.abbreviated).day().hour().minute())) }
        return parts.joined(separator: " · ")
    }
}

/// Safety, inline and opaque: a warning symbol and a direct heading (the first note's instruction), then every
/// backend note in full. Never delayed, animated or dismissed (DESIGN §6: safety is immediate and persistent).
struct StageSafetyCard: View {
    let notes: [ReportModel.SafetyNote]

    var body: some View {
        if let heading = ReportWords.safetyLine(notes) {
            VStack(alignment: .leading, spacing: Spacing.s) {
                Label {
                    Text(heading).display(.card).foregroundStyle(Palette.danger)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: Symbol.safety).foregroundStyle(Palette.danger)
                }
                .accessibilityAddTraits(.isHeader)
                ForEach(notes) { note in
                    Text(ReportWords.display(note.text)).font(.subheadline).foregroundStyle(Palette.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .card(tone: .danger)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Safety: \(heading) \(notes.map { ReportWords.display($0.text) }.joined(separator: " "))")
        }
    }
}

/// The report's previous name for the safety card.
typealias StageSafetyLine = StageSafetyCard

/// A likely problem as a specimen label: its `Marker` and the read-only severity tag; the part in `cardTitle`, the
/// issue in the tone; the crop on the right (true colour); ruled rows for where and evidence; confidence as a word.
struct StageProblemRow: View {
    let problem: ReportModel.Problem
    let number: Int
    /// true: tapping flies the stage to its marker; false: opens the detail.
    var located: Bool
    var selected = false

    var body: some View {
        SpecimenCard(number: number, tone: problem.severity.tone, tag: ToneTag.severity(problem.severity),
                     part: problem.part, issue: problem.issue, crop: problem.crops.first,
                     rows: rows, footnote: footnote, located: located, selected: selected)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(number): \(problem.part), \(problem.issue). \(spoken)")
            .accessibilityHint(located ? "Shows it on the scan" : "Shows the photo and the evidence")
    }

    private var rows: [RuledRows.Row] {
        var out: [RuledRows.Row] = []
        if let w = problem.whereWords?.trimmedOrNil { out.append(.init("Where", w)) }
        return out
    }

    /// One hedge only: "Confirmed with a meter", else the confidence word, plus how often it was seen.
    private var footnote: String? {
        if problem.proved { return "Confirmed with a meter" }
        var parts: [String] = []
        if let c = problem.confidence { parts.append(c.lowercased().capitalizedFirst) }
        if problem.regions > 1 { parts.append("Seen \(problem.regions) times") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private var spoken: String {
        [problem.severity?.words, problem.whereWords, footnote].compactMap { $0 }.joined(separator: ". ")
    }
}

/// A setup row (report `setup`: a battery missing, a cable unplugged): not broken, so the calm setup tone with a
/// wrench and the word "Setup", and its fix in full. Numbered after the problems, like its marker on the stage.
struct StageSetupRow: View {
    let problem: ReportModel.Problem
    let number: Int
    /// true: tapping flies the stage to its marker; false: opens the detail.
    var located: Bool
    var selected = false

    var body: some View {
        SpecimenCard(number: number, tone: .setup, tag: ToneTag(text: "Setup", tone: .setup, symbol: Symbol.setup),
                     part: problem.part, issue: problem.issue, crop: problem.crops.first,
                     rows: problem.fix.map { [RuledRows.Row("What to do", ReportWords.display($0))] } ?? [],
                     footnote: nil, located: located, selected: selected)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(ReportWords.setupLabel(problem, number: number))
            .accessibilityHint(located ? "Shows it on the scan" : "Shows the photo and the evidence")
    }
}

/// The shared specimen label layout for problem and setup cards. Selected: a 2 pt accent outline.
private struct SpecimenCard: View {
    let number: Int
    let tone: Tone
    let tag: ToneTag?
    let part: String
    let issue: String
    let crop: String?
    let rows: [RuledRows.Row]
    let footnote: String?
    let located: Bool
    let selected: Bool
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .center) {
                Marker(number: number, tone: tone)
                Spacer(minLength: Spacing.s)
                tag
            }
            let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.s))
                : AnyLayout(HStackLayout(alignment: .top, spacing: Spacing.m))
            layout {
                VStack(alignment: .leading, spacing: 2) {
                    Text(part).display(.card).foregroundStyle(Palette.ink)
                    Text(issue).font(.subheadline.weight(.semibold)).foregroundStyle(tone.text)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .multilineTextAlignment(.leading)
                if crop != nil {
                    CropThumb(sha: crop, cornerRadius: Radius.small)
                        .frame(width: typeSize.isAccessibilitySize ? nil : 56, height: typeSize.isAccessibilitySize ? 140 : 56)
                        .frame(maxWidth: typeSize.isAccessibilitySize ? .infinity : nil)
                }
            }
            if !rows.isEmpty { RuledRows(rows: rows) }
            HStack(spacing: Spacing.xs) {
                Spacer(minLength: 0)
                if let footnote { Text(footnote).font(.judgement).foregroundStyle(Palette.secondary) }
                Image(systemName: located ? "scope" : "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(located ? Palette.accent : Palette.secondary)
                    .accessibilityHidden(true)
            }
        }
        .card(padding: 14)
        .overlay {
            RoundedRectangle(cornerRadius: Radius.card, style: .continuous)
                .strokeBorder(Palette.accent, lineWidth: 2)
                .opacity(selected ? 1 : 0)
        }
        .motion(Motion.fade, value: selected)
    }
}

/// "What wasn't checked": not inspected and could-not-judge as one list, off the default path (the report's ⋯ menu,
/// or ask Lea). Honest by construction: every row is shown with its short reason, dashed (unknown), never counted as
/// fine; the backend's limits follow as quiet footnotes; Continue scanning when it can go back for them.
struct WhatWasntCheckedSheet: View {
    let notInspected: [ReportModel.Gap]
    let unclear: [ReportModel.Gap]
    var limits: [String] = []
    var continueScan: (() -> Void)?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.l) {
                    Text("What wasn't checked").display(.screen).foregroundStyle(Palette.ink)
                        .accessibilityAddTraits(.isHeader)
                    let summary = ReportWords.gapSummary(notInspected: notInspected.count, unclear: unclear.count)
                    if !summary.isEmpty {
                        Text(summary).font(.body).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    if !rows.isEmpty {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            ForEach(Array(rows.enumerated()), id: \.element.id) { i, row in
                                if i > 0 { Rectangle().fill(Palette.hairline).frame(height: Line.hairline).padding(.leading, 64) }
                                GapRow(gap: row.gap, unclear: row.unclear)
                            }
                        }
                        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: Line.hairline))
                    }
                    ForEach(Array(limits.enumerated()), id: \.offset) { _, line in
                        Text(ReportWords.display(line)).font(.footnote).foregroundStyle(Palette.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(Spacing.gutter)
            }
            .background(Palette.canvas.ignoresSafeArea())
            .safeAreaInset(edge: .bottom) {
                if let continueScan {
                    GlassBar {
                        Button { dismiss(); continueScan() } label: { Label("Continue scanning", systemImage: Symbol.scan) }
                            .buttonStyle(.primary)
                    }
                }
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.fontWeight(.semibold) } }
        }
        .tint(Palette.accent)
        .presentationDetents([.medium, .large])
    }

    fileprivate struct Row: Identifiable { let id: String; let gap: ReportModel.Gap; let unclear: Bool }

    private var rows: [Row] {
        unclear.map { Row(id: "u" + $0.id, gap: $0, unclear: true) } + notInspected.map { Row(id: "n" + $0.id, gap: $0, unclear: false) }
    }
}

/// One "not checked" row: a dashed tile (or the real crop), the area, and the backend's short reason.
private struct GapRow: View {
    let gap: ReportModel.Gap
    let unclear: Bool

    var body: some View {
        HStack(alignment: .center, spacing: Spacing.m) {
            if let crop = gap.crop {
                CropThumb(sha: crop, cornerRadius: Radius.small).frame(width: 36, height: 36)
                    .overlay(RoundedRectangle(cornerRadius: Radius.small, style: .continuous)
                        .strokeBorder(Palette.secondary.opacity(0.7), style: StrokeStyle(lineWidth: 1, dash: Line.dash)))
            } else {
                DashedTile(symbol: unclear ? Symbol.couldNotJudge : Symbol.notInspected)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(ReportWords.gapTitle(gap)).font(.subheadline.weight(.semibold)).foregroundStyle(Palette.ink)
                Text(ReportWords.display(ReportWords.gapReason(gap, unclear: unclear))).font(.footnote).foregroundStyle(Palette.secondary)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
    }
}

/// The report's previous inline "What Lea couldn't check" card, kept until Stage B moves the report onto the sheet
/// (C5): a compact quiet row that opens `WhatWasntCheckedSheet`. No caveat sentence on the default path.
struct CouldNotCheckCard: View {
    let notInspected: [ReportModel.Gap]
    let unclear: [ReportModel.Gap]
    var continueScan: (() -> Void)?
    @State private var showAll = false

    var body: some View {
        let total = notInspected.count + unclear.count
        if total > 0 {
            GroupedList {
                Button { showAll = true } label: {
                    HStack(spacing: Spacing.m) {
                        DashedTile(symbol: Symbol.couldNotJudge)
                        Text("What wasn't checked").font(.subheadline.weight(.semibold)).foregroundStyle(Palette.ink)
                        Spacer(minLength: 0)
                        Text("\(total)").font(.label).monospacedDigit().foregroundStyle(Palette.secondary)
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
                    }
                    .padding(.vertical, Spacing.s)
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableCardStyle())
            }
            .sheet(isPresented: $showAll) {
                WhatWasntCheckedSheet(notInspected: notInspected, unclear: unclear, continueScan: continueScan)
            }
        }
    }
}
