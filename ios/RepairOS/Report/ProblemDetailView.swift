import SwiftUI

/// One problem in full (DESIGN §7.6, screen 18): as a sheet, a 44 pt row with previous and next (the problems in
/// marker order), where you are ("1 of 2") and Done; then the part, the issue in its tone and the read-only severity
/// tag; the evidence photo first (the full still with the defect's box as a tone corner set, a leader to its `Marker`,
/// and the still's real label), or the crops when there is no still; ruled rows for where, confidence, likelihood and
/// evidence; what the photo shows; what to do. Tap the photo to zoom. No per-problem caveat (owner, 2026-10-25): the
/// words for confidence and likelihood, and the backend's own "looks / may be", carry it; "Confirmed with a meter"
/// shows when a reading proved it.
struct ProblemDetailView: View {
    let problems: [ReportModel.Problem]
    @State private var index: Int
    /// The session's stills (`stills/<still_id>.jpg`) for the photo-first view; nil shows the crops.
    var stillsDir: URL?
    /// Each problem's number on the stage (most likely, then setup), when it has one.
    var numbers: [String: Int] = [:]
    /// Shown as a sheet: the previous / next row with Done. nil when pushed (the system back button).
    var onDone: (() -> Void)?

    @State private var still: StillImage?
    @State private var page = 0
    @State private var zoomed: DetailZoom?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(problems: [ReportModel.Problem], index: Int = 0, stillsDir: URL? = nil, numbers: [String: Int] = [:],
         onDone: (() -> Void)? = nil) {
        self.problems = problems
        _index = SwiftUI.State(initialValue: min(max(0, index), max(0, problems.count - 1)))
        self.stillsDir = stillsDir
        self.numbers = numbers
        self.onDone = onDone
    }

    /// One problem on its own (pushed from a row, or from the AR view).
    init(problem: ReportModel.Problem) {
        self.init(problems: [problem])
    }

    private var problem: ReportModel.Problem { problems[index] }

    var body: some View {
        VStack(spacing: 0) {
            if let onDone { pager(onDone) }
            ScrollView {
                content.id(problem.id)
                    .transition(.opacity)
            }
            .bounceBasedOnSize()
        }
        .background(Palette.canvas.ignoresSafeArea())
        .navigationTitle(onDone == nil ? problem.part : "")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(onDone == nil ? .automatic : .hidden, for: .navigationBar)
        .task(id: problem.id) {
            still = nil
            page = 0
            guard let dir = stillsDir, let id = problem.cropStill else { return }
            let loaded = await StillImage.load(dir.appending(path: "\(id).jpg"), maxPixels: 1600)
            withAnimation(Motion.fade) { still = loaded }
        }
        .fullScreenCover(item: $zoomed) { z in CropZoomView(shas: problem.crops, index: z.index) }
        .tint(Palette.accent)
    }

    /// ‹ › to the neighbouring problems in place, the position, and Done.
    private func pager(_ done: @escaping () -> Void) -> some View {
        HStack(spacing: Spacing.s) {
            if problems.count > 1 {
                Button { go(-1) } label: { Image(systemName: "chevron.left") }
                    .buttonStyle(RoundIconButtonStyle())
                    .disabled(index == 0).opacity(index == 0 ? 0.4 : 1)
                    .accessibilityLabel("Previous problem")
                Button { go(1) } label: { Image(systemName: "chevron.right") }
                    .buttonStyle(RoundIconButtonStyle())
                    .disabled(index >= problems.count - 1).opacity(index >= problems.count - 1 ? 0.4 : 1)
                    .accessibilityLabel("Next problem")
                Text("\(index + 1) of \(problems.count)").font(.label).monospacedDigit().foregroundStyle(Palette.secondary)
                    .contentTransition(.numericText())
            }
            Spacer()
            Button("Done", action: done).font(.body.weight(.semibold)).frame(minHeight: 44)
        }
        .padding(.horizontal, Spacing.gutter)
        .padding(.top, Spacing.m)
        .haptic(Haptic.select, trigger: index)
    }

    private func go(_ step: Int) {
        let next = index + step
        guard problems.indices.contains(next) else { return }
        withAnimation(reduceMotion ? nil : Motion.fade) { index = next }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: Spacing.xl) {
            VStack(alignment: .leading, spacing: Spacing.xs) {
                Text(problem.part).display(.screen).foregroundStyle(Palette.ink)
                    .accessibilityAddTraits(.isHeader)
                Text(problem.issue).font(.title3.weight(.semibold)).foregroundStyle(problem.tone.text)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: Spacing.m) {
                    if problem.isSetup && problem.severity != .safety {
                        ToneTag(text: "Setup", tone: .setup, symbol: Symbol.setup)
                    } else {
                        ToneTag.severity(problem.severity)
                    }
                    if problem.proved { ToneTag(text: "Confirmed with a meter", tone: .neutral, symbol: Symbol.good) }
                }
                .padding(.top, Spacing.xs)
            }
            evidence
            if !rows.isEmpty { RuledRows(rows: rows) }
            if let fix = problem.fix {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text(problem.isSetup ? "How to set it right" : "What to do").display(.card).foregroundStyle(Palette.ink)
                    Text(ReportWords.display(fix)).font(.body).foregroundStyle(Palette.ink).fixedSize(horizontal: false, vertical: true)
                    if problem.isSetup && problem.severity != .safety {
                        Text(ReportWords.setupNote).font(.footnote).foregroundStyle(Palette.secondary)
                    }
                }
                .accessibilityElement(children: .combine)
            }
            if let observation = problem.observation {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text("What the photo shows").display(.card).foregroundStyle(Palette.ink)
                    Text(ReportWords.display(observation)).font(.body).foregroundStyle(Palette.ink).lineSpacing(2)
                        .textSelection(.enabled)
                }
            }
        }
        .padding(.horizontal, Spacing.gutter)
        .padding(.top, Spacing.m)
        .padding(.bottom, Spacing.xxl)
    }

    private var rows: [RuledRows.Row] {
        var out: [RuledRows.Row] = []
        if let w = problem.whereWords?.trimmedOrNil { out.append(.init("Where", w)) }
        if let c = problem.confidence { out.append(.init("Confidence", c.replacingOccurrences(of: " confidence", with: ""))) }
        if let l = problem.likelihood { out.append(.init("Likelihood", l)) }
        return out
    }

    /// The photo first: the full still with its box when there is one, else the crops (a pager for several).
    @ViewBuilder private var evidence: some View {
        if let still, let box = normalizedBox(still) {
            Button { zoomed = DetailZoom(index: 0) } label: {
                EvidencePhoto(image: still.upright, box: box, tone: problem.tone, number: numbers[problem.id],
                              caption: nil, height: 320)
            }
            .buttonStyle(PressableCardStyle())
            .accessibilityHint("Opens the photo full screen")
        } else if problem.crops.count > 1 {
            TabView(selection: $page) {
                ForEach(Array(problem.crops.enumerated()), id: \.offset) { i, sha in
                    zoomable(sha, at: i).padding(.horizontal, 2).tag(i)
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .always))
            .aspectRatio(1, contentMode: .fit)
        } else if let sha = problem.crops.first {
            zoomable(sha, at: 0).aspectRatio(1, contentMode: .fit)
        }
    }

    /// The crop's box (`box_px`, sensor pixels) as fractions of the upright still (turned 90° clockwise).
    private func normalizedBox(_ s: StillImage) -> CGRect? {
        guard let b = problem.cropBox, b.count == 4, s.full.width > 0, s.full.height > 0 else { return nil }
        let w = s.full.width, h = s.full.height
        return CGRect(x: (h - b[3]) / h, y: b[0] / w, width: (b[3] - b[1]) / h, height: (b[2] - b[0]) / w)
    }

    private func zoomable(_ sha: String, at i: Int) -> some View {
        CropThumb(sha: sha, cornerRadius: Radius.card)
            .overlay(alignment: .bottomTrailing) {
                Image(systemName: "arrow.up.left.and.arrow.down.right").font(.footnote.weight(.semibold))
                    .foregroundStyle(Palette.chalk).padding(8).background(Palette.smoke, in: Circle()).padding(Spacing.m)
            }
            .contentShape(Rectangle())
            .onTapGesture { zoomed = DetailZoom(index: i) }
            .accessibilityElement()
            .accessibilityLabel("Evidence photo \(i + 1)")
            .accessibilityHint("Opens the photo full screen")
            .accessibilityAddTraits(.isButton)
    }
}

private struct DetailZoom: Identifiable {
    let index: Int
    var id: Int { index }
}
