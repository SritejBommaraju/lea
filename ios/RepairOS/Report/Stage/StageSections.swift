import SwiftUI

// The pieces of the report sheet that rises over the dark stage: the verdict (counts eyebrow, the top finding as
// the headline, one honest sentence), safety as one line with "Why", problem rows numbered like the markers, and
// one "What Lea couldn't check" summary. Words come from ReportWords (report fields only).

/// Eyebrow, headline, sentence, then the person's own words and the date.
struct StageVerdictHeader: View {
    let report: ReportModel
    var symptom: String?

    var body: some View {
        let tone = ReportWords.tone(report)
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(spacing: 6) {
                Circle().fill(tone.color).frame(width: 8, height: 8).accessibilityHidden(true)
                Text(ReportWords.eyebrow(report))
                    .font(.system(.caption, design: .rounded, weight: .semibold))
                    .tracking(0.8)
                    .textCase(.uppercase)
                    .foregroundStyle(tone.text)
            }
            Text(ReportWords.headline(report))
                .font(.system(.title, design: .rounded, weight: .bold))
                .tracking(-0.4)
                .fixedSize(horizontal: false, vertical: true)
            if let sentence = ReportWords.sentence(report) {
                Text(sentence).font(.body).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if !meta.isEmpty {
                Text(meta).font(.footnote).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    private var meta: String {
        var parts: [String] = []
        if let symptom = symptom?.trimmedOrNil { parts.append("\u{201C}\(symptom)\u{201D}") }
        if report.photos > 0 {
            parts.append(report.photosInspected == report.photos ? "\(report.photos) photos" : "\(report.photosInspected) of \(report.photos) photos checked")
        }
        if let date = report.createdAt { parts.append(date.formatted(.dateTime.month(.abbreviated).day().hour().minute())) }
        return parts.joined(separator: " · ")
    }
}

/// Safety as one line: the first sentence of the first note, then "Why" for all of them. Expanded at AX sizes.
struct StageSafetyLine: View {
    let notes: [ReportModel.SafetyNote]
    @State private var showAll = false
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        if let line = ReportWords.safetyLine(notes) {
            Group {
                if typeSize.isAccessibilitySize {
                    VStack(alignment: .leading, spacing: Spacing.s) { allNotes }
                } else {
                    Button { showAll = true } label: {
                        HStack(spacing: Spacing.m) {
                            Image(systemName: notes.first?.symbol ?? Symbol.safety).foregroundStyle(Palette.danger)
                                .accessibilityHidden(true)
                            Text(line).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                                .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: Spacing.s)
                            Text("Why").font(.subheadline.weight(.semibold)).foregroundStyle(Palette.danger)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableCardStyle())
                    .accessibilityLabel("Safety: \(line)")
                    .accessibilityHint("Shows every safety note")
                }
            }
            .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
            .frame(minHeight: 52)
            .background(Palette.danger.opacity(0.12), in: RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
            .sheet(isPresented: $showAll) {
                NavigationStack {
                    ScrollView { VStack(alignment: .leading, spacing: Spacing.l) { allNotes }.padding(Spacing.gutter) }
                        .navigationTitle("Before you touch it").navigationBarTitleDisplayMode(.inline)
                        .toolbar { Button("Done") { showAll = false }.fontWeight(.semibold) }
                }
                .presentationDetents([.medium, .large])
            }
        }
    }

    @ViewBuilder private var allNotes: some View {
        ForEach(notes) { note in
            HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                Image(systemName: note.symbol).foregroundStyle(Palette.danger).frame(width: 22).accessibilityHidden(true)
                Text(ReportWords.sanitize(note.text)).font(.body).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// A likely problem, numbered and coloured like its marker on the stage.
struct StageProblemRow: View {
    let problem: ReportModel.Problem
    let number: Int
    /// true: tapping flies the stage to its marker (a target glyph); false: opens the detail (a chevron).
    var located: Bool
    var selected = false
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        let tone = problem.severity.tone
        let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.m))
            : AnyLayout(HStackLayout(alignment: .center, spacing: 14))
        layout {
            CropThumb(sha: problem.crops.first)
                .frame(width: typeSize.isAccessibilitySize ? nil : 64, height: typeSize.isAccessibilitySize ? 160 : 64)
                .overlay(alignment: .topLeading) { StageNumber(number: number, tone: tone).offset(x: -5, y: -5) }
            VStack(alignment: .leading, spacing: 3) {
                Text(problem.part).font(.headline).foregroundStyle(.primary)
                Text(problem.issue).font(.subheadline.weight(.semibold)).foregroundStyle(tone.text)
                Text(meta).font(.footnote).foregroundStyle(.secondary).lineLimit(typeSize.isAccessibilitySize ? nil : 1)
            }
            .multilineTextAlignment(.leading)
            Spacer(minLength: 0)
            Image(systemName: located ? "scope" : "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(located ? AnyShapeStyle(Palette.accent) : AnyShapeStyle(.tertiary))
                .accessibilityHidden(true)
        }
        .card(padding: 14)
        .overlay {
            RoundedRectangle(cornerRadius: Radius.card, style: .continuous)
                .strokeBorder(Palette.accent, lineWidth: 2)
                .opacity(selected ? 1 : 0)
        }
        .motion(Motion.fade, value: selected)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(number): \(problem.part), \(problem.issue). \(meta)")
        .accessibilityHint(located ? "Shows it on the scan" : "Shows the photo and the evidence")
    }

    /// One hedge only: severity, then confidence, then "Seen N times" / "Measured".
    private var meta: String {
        var parts: [String] = []
        if let s = problem.severity { parts.append(s.words) }
        if let c = problem.confidence { parts.append(c.lowercased().capitalizedFirst) }
        if problem.proved { parts.append("Measured") } else if problem.regions > 1 { parts.append("Seen \(problem.regions) times") }
        return parts.joined(separator: " · ")
    }
}

/// A setup row (report `setup`: a battery missing, a cable unplugged): not broken, so the calm setup tone, and its fix
/// in full. Numbered like its marker on the stage (StageMarks).
struct StageSetupRow: View {
    let problem: ReportModel.Problem
    let number: Int
    /// true: tapping flies the stage to its marker (a target glyph); false: opens the detail (a chevron).
    var located: Bool
    var selected = false
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.m))
            : AnyLayout(HStackLayout(alignment: .top, spacing: 14))
        layout {
            CropThumb(sha: problem.crops.first)
                .frame(width: typeSize.isAccessibilitySize ? nil : 56, height: typeSize.isAccessibilitySize ? 140 : 56)
                .overlay(alignment: .topLeading) { StageNumber(number: number, tone: .setup).offset(x: -5, y: -5) }
            VStack(alignment: .leading, spacing: 3) {
                Text(problem.part).font(.headline).foregroundStyle(.primary)
                Text(problem.issue).font(.subheadline.weight(.semibold)).foregroundStyle(Tone.setup.text)
                if let fix = problem.fix {
                    Text(ReportWords.sanitize(fix)).font(.subheadline).foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 2)
                }
            }
            .multilineTextAlignment(.leading)
            Spacer(minLength: 0)
            Image(systemName: located ? "scope" : "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(located ? AnyShapeStyle(Palette.setup) : AnyShapeStyle(.tertiary))
                .accessibilityHidden(true)
        }
        .card(padding: 14)
        .overlay {
            RoundedRectangle(cornerRadius: Radius.card, style: .continuous)
                .strokeBorder(Palette.setup, lineWidth: 2)
                .opacity(selected ? 1 : 0)
        }
        .motion(Motion.fade, value: selected)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(ReportWords.setupLabel(problem, number: number))
        .accessibilityHint(located ? "Shows it on the scan" : "Shows the photo and the evidence")
    }
}

/// The number badge used on rows; the 3D markers draw the same disc (tone fill, white ring, rounded bold number).
struct StageNumber: View {
    let number: Int
    let tone: Tone
    @ScaledMetric(relativeTo: .caption) private var size: CGFloat = 22

    var body: some View {
        Text("\(number)")
            .font(.system(.caption, design: .rounded, weight: .bold)).monospacedDigit()
            .foregroundStyle(.white)
            .frame(minWidth: size, minHeight: size)
            .background(tone.color, in: Circle())
            .overlay(Circle().strokeBorder(.white, lineWidth: 1.5))
            .accessibilityHidden(true)
    }
}

/// "What Lea couldn't check": not inspected and could-not-judge as one summary with the action that fixes it.
/// Honest by construction: they are never counted as fine, and every row stays reachable under "Show all".
struct CouldNotCheckCard: View {
    let notInspected: [ReportModel.Gap]
    let unclear: [ReportModel.Gap]
    var continueScan: (() -> Void)?
    @State private var showAll = false

    private struct Row: Identifiable { let id: String; let gap: ReportModel.Gap; let unclear: Bool }

    private var rows: [Row] {
        unclear.map { Row(id: "u" + $0.id, gap: $0, unclear: true) } + notInspected.map { Row(id: "n" + $0.id, gap: $0, unclear: false) }
    }

    var body: some View {
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Label("What Lea couldn't check", systemImage: Symbol.couldNotJudge)
                        .font(.headline)
                    Text(ReportWords.gapSummary(notInspected: notInspected.count, unclear: unclear.count))
                        .font(.subheadline).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    if let continueScan {
                        Button(action: continueScan) { Label("Continue scanning", systemImage: Symbol.scan) }
                            .buttonStyle(.primary(.quiet))
                            .padding(.top, Spacing.xs)
                    }
                }
                .padding(Spacing.l)
                .accessibilityElement(children: .contain)
                Divider()
                Button { showAll = true } label: {
                    HStack {
                        Text("Show all \(rows.count)")
                        Spacer()
                        Image(systemName: "chevron.right")
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Palette.accent)
                    .padding(Spacing.l)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: 0.5))
            .clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
            .sheet(isPresented: $showAll) {
                // Hundreds of rows are possible: a lazy List in a sheet, not an expanding stack in the report.
                NavigationStack {
                    List(rows) { row in GapRow(gap: row.gap, unclear: row.unclear).listRowInsets(EdgeInsets()) }
                        .listStyle(.plain)
                        .navigationTitle("What Lea couldn't check").navigationBarTitleDisplayMode(.inline)
                        .toolbar { Button("Done") { showAll = false }.fontWeight(.semibold) }
                }
                .presentationDetents([.medium, .large])
            }
        }
    }

    private struct GapRow: View {
        let gap: ReportModel.Gap
        let unclear: Bool

        var body: some View {
            HStack(alignment: .center, spacing: Spacing.m) {
                if let crop = gap.crop {
                    CropThumb(sha: crop, cornerRadius: Radius.small).frame(width: 44, height: 44)
                } else {
                    Image(systemName: unclear ? Symbol.couldNotJudge : Symbol.notInspected).foregroundStyle(.secondary)
                        .frame(width: 44, height: 44)
                        .background(Palette.well, in: RoundedRectangle(cornerRadius: Radius.small, style: .continuous))
                        .accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(ReportWords.gapTitle(gap)).font(.subheadline.weight(.semibold))
                    Text(ReportWords.gapReason(gap, unclear: unclear)).font(.footnote).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
            .accessibilityElement(children: .combine)
        }
    }
}
