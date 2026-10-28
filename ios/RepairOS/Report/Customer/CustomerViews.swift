import SwiftUI
import UIKit

/// What the customer-facing pieces of the report need from the screen that shows them: which scan, the gateway to
/// fetch pictures and files from and send answers to, the board that keeps the answers, and how to open the meter.
struct CustomerContext {
    let scanID: String
    let gateway: GatewayClient?
    let outcomes: OutcomeBoard
    /// Bumped each time the design is rebuilt, so its picture is fetched again.
    var designRevision = 0
    /// Opens the meter sheet for the check with this action id (nil: not tied to a check).
    let openMeter: (String?) -> Void
}

// MARK: - The design's picture

/// The design's own picture of the product (the gateway fetches it from the design engine), so the customer can set
/// what they hold beside what it should look like. When there is no picture the view is simply not there: no
/// placeholder, no error.
struct DesignPicture: View {
    let scanID: String
    let gateway: GatewayClient?
    /// Bumped when the design was changed ("Fix it with Grain" rebuilt it): the picture is fetched again.
    var revision = 0
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Image(uiImage: image)
                        .resizable().scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: 220)
                        .padding(Spacing.m)
                        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous)
                            .strokeBorder(Palette.hairline, lineWidth: Line.hairline))
                        .accessibilityLabel("The design's picture of your product")
                    Text("What it should look like").font(.footnote).foregroundStyle(Palette.secondary)
                        .accessibilityHidden(true)
                }
                .transition(.opacity)
            }
        }
        .task(id: "\(scanID)#\(revision)") {
            guard let gateway else { return }
            guard let data = try? await gateway.designView(scanID: scanID), let loaded = UIImage(data: data) else { return }
            withAnimation(Motion.fade) { image = loaded }
        }
    }
}

// MARK: - One flagged part: what we saw, what to do, did it work

/// The card under a flagged part: what the scan or the photos showed, how sure that is (in words), the fix as a
/// button or a list, and "Did this fix it?". Everything about the fix comes from the report row's `fix_plan`; a row
/// with only a one-sentence `fix` shows that sentence.
struct FixPlanCard: View {
    let problem: ReportModel.Problem
    let customer: CustomerContext
    @ObservedObject var outcomes: OutcomeBoard
    @State private var shared: SharedFile?
    @State private var fetching: String?
    @State private var fileError: String?
    @Environment(\.openURL) private var openURL

    init(problem: ReportModel.Problem, customer: CustomerContext) {
        self.problem = problem
        self.customer = customer
        _outcomes = ObservedObject(wrappedValue: customer.outcomes)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("Fix for \(CustomerWords.thePart(problem))").display(.card).foregroundStyle(Palette.ink)
                .accessibilityAddTraits(.isHeader)
            sawBlock
            fixBlock
            OutcomeBar(rowID: problem.id, key: problem.subjectKey, part: CustomerWords.thePart(problem),
                       server: problem.outcome, board: outcomes)
        }
        .card(padding: 14)
        .sheet(item: $shared) { file in ShareSheet(url: file.url) }
    }

    // MARK: What we saw

    private var evidenceLines: [String] {
        var seen = Set<String>()
        return ([problem.observation] + problem.evidence.map(Optional.some)).compactMap { $0 }
            .map { ReportWords.display($0) }.compactMap(\.trimmedOrNil)
            .filter { seen.insert($0).inserted }
    }

    @ViewBuilder private var sawBlock: some View {
        let lines = Array(evidenceLines.prefix(3))
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text("What we saw").font(.label).foregroundStyle(Palette.secondary)
            HStack(alignment: .top, spacing: Spacing.m) {
                if let crop = problem.crops.first {
                    CropThumb(sha: crop, cornerRadius: Radius.small).frame(width: 64, height: 64)
                }
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    ForEach(lines.indices, id: \.self) { i in
                        Text(lines[i]).font(.subheadline).foregroundStyle(Palette.ink).fixedSize(horizontal: false, vertical: true)
                    }
                    Text(CustomerWords.evidenceSource(problem)).font(.footnote).foregroundStyle(Palette.secondary)
                    if let sure = CustomerWords.confidenceLine(problem.confidenceWord, proved: problem.proved) {
                        Text(sure).font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
                    }
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: The fix

    @ViewBuilder private var fixBlock: some View {
        let plan = problem.fixPlan
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("What to do").font(.label).foregroundStyle(Palette.secondary)
            if let line = (plan?.summary ?? problem.fix)?.trimmedOrNil {
                Text(ReportWords.display(line)).font(.body.weight(.semibold)).foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
            switch plan?.action {
            case .buy:
                ForEach(plan?.offers ?? []) { offer in offerRow(offer) }
                if (plan?.offers ?? []).isEmpty {
                    Text("No shop link yet. Search for the part name above.").font(.footnote).foregroundStyle(Palette.secondary)
                }
            case .remake:
                remakeButtons(plan?.fileFormats ?? [])
                steps(plan?.steps ?? [])
            case .reseat, .rewire:
                steps(plan?.steps ?? [])
            case .check:
                Button { customer.openMeter(plan?.checkActionID) } label: {
                    Label("Check with your phone", systemImage: "gauge.with.dots.needle.50percent")
                }
                .buttonStyle(.primary)
            case nil:
                EmptyView()
            }
        }
    }

    private func steps(_ list: [String]) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            ForEach(list.indices, id: \.self) { i in
                HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                    Text("\(i + 1).").font(.subheadline.weight(.semibold)).foregroundStyle(Palette.secondary).monospacedDigit()
                    Text(list[i]).font(.subheadline).foregroundStyle(Palette.ink).fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
        }
    }

    private func offerRow(_ o: FixIt.Option) -> some View {
        Button { openURL(o.link) } label: {
            HStack(spacing: Spacing.m) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(o.title).font(.subheadline).foregroundStyle(Palette.ink).lineLimit(2).multilineTextAlignment(.leading)
                    Text(o.seller).font(.footnote).foregroundStyle(Palette.secondary).lineLimit(1)
                }
                Spacer(minLength: Spacing.s)
                if let price = o.priceWords {
                    Text(price).font(.subheadline.weight(.semibold)).monospacedDigit().foregroundStyle(Palette.ink)
                }
                Image(systemName: "arrow.up.right").font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
            }
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableCardStyle())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens the shop in Safari")
    }

    @ViewBuilder private func remakeButtons(_ formats: [String]) -> some View {
        if formats.isEmpty {
            EmptyView()
        } else {
            ForEach(formats, id: \.self) { format in
                Button { Task { await download(format) } } label: {
                    HStack {
                        Label(formats.count > 1 ? "Download the part file (\(format.uppercased()))" : "Download the part file",
                              systemImage: "square.and.arrow.down")
                        if fetching == format { ProgressView().controlSize(.small) }
                    }
                }
                .buttonStyle(.primary)
                .disabled(fetching != nil)
            }
            if let fileError { Text(fileError).font(.footnote).foregroundStyle(Palette.danger) }
        }
    }

    private func download(_ format: String) async {
        guard let gateway = customer.gateway else { fileError = "Connect to the server in Settings first."; return }
        fetching = format
        fileError = nil
        defer { fetching = nil }
        do {
            let (data, name) = try await gateway.partFile(scanID: customer.scanID, rowID: problem.id, format: format)
            let dir = URL.temporaryDirectory.appending(path: "part-\(UUID().uuidString)")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let url = dir.appending(path: name.replacingOccurrences(of: "/", with: "_"))
            try data.write(to: url, options: .atomic)
            shared = SharedFile(url: url)
        } catch GatewayError.http(404, _) {
            fileError = "The design has no \(format.uppercased()) file for this part."
        } catch {
            fileError = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}

struct SharedFile: Identifiable {
    let id = UUID()
    let url: URL
}

/// The system share sheet for a file (save to Files, AirDrop, open in a slicer).
struct ShareSheet: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}

// MARK: - Did this fix it?

/// "Did this fix it?" Yes, No or Haven't tried: one tap saves the answer (this is the label the design engine learns
/// from). The three buttons are at least 44 pt, an answer that is already given can only be changed through a
/// confirmation, and the line under them says whether it is saved, waiting for the network, or refused.
struct OutcomeBar: View {
    let rowID: String
    /// What the row is about: an answer saved under an older id of the same part moves to this row.
    var key: String? = nil
    /// "the rear wheel", for the confirmation and for VoiceOver.
    let part: String
    let server: OutcomeAnswer?
    @ObservedObject var board: OutcomeBoard
    @State private var asking: OutcomeChoice?

    private var current: OutcomeChoice? { board.answer(rowID: rowID, server: server) }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("Did this fix it?").font(.headline).foregroundStyle(Palette.ink).accessibilityAddTraits(.isHeader)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: Spacing.s) { buttons }
                VStack(spacing: Spacing.s) { buttons }
            }
            statusLine
        }
        .padding(.top, Spacing.xs)
        .onAppear { if let key { board.adopt(rowID: rowID, key: key) } }
        .confirmationDialog("Change your answer?", isPresented: Binding(get: { asking != nil }, set: { if !$0 { asking = nil } }),
                            titleVisibility: .visible, presenting: asking) { choice in
            Button("Change to \u{201C}\(choice.button)\u{201D}") { board.set(choice, rowID: rowID, key: key) }
            Button("Keep my answer", role: .cancel) {}
        } message: { choice in
            Text("You said \u{201C}\((current ?? choice).button)\u{201D}. Change it to \u{201C}\(choice.button)\u{201D}?")
        }
    }

    @ViewBuilder private var buttons: some View {
        ForEach(OutcomeChoice.allCases, id: \.self) { choice in
            let selected = current == choice
            Button { tap(choice) } label: {
                Text(choice.button)
                    .font(.body.weight(.semibold))
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .padding(.horizontal, Spacing.s)
                    .foregroundStyle(selected ? Palette.onAccent : Palette.ink)
                    .background(selected ? Palette.accent : Palette.well,
                                in: RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
                    .contentShape(RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
            }
            .buttonStyle(PressableCardStyle())
            .accessibilityLabel("Did the fix for \(part) work? \(choice.button)")
            .accessibilityAddTraits(selected ? .isSelected : [])
        }
    }

    @ViewBuilder private var statusLine: some View {
        if let status = board.status[rowID], current != nil {
            HStack(spacing: Spacing.s) {
                Text(status.words).font(.footnote).foregroundStyle(status.isProblem ? Palette.danger : Palette.secondary)
                if status.isProblem {
                    Button("Try again") { Task { await board.flush() } }
                        .font(.footnote.weight(.semibold)).frame(minHeight: 44)
                }
            }
            .accessibilityElement(children: .combine)
        } else if current != nil {
            Text(OutcomeStatus.saved.words).font(.footnote).foregroundStyle(Palette.secondary)
        } else {
            Text("Your answer helps Grain learn which fixes work.").font(.footnote).foregroundStyle(Palette.secondary)
        }
    }

    private func tap(_ choice: OutcomeChoice) {
        guard let existing = current else { board.set(choice, rowID: rowID, key: key); return }
        if existing != choice { asking = choice }
    }
}

extension OutcomeStatus {
    var isProblem: Bool { if case .failed = self { true } else { false } }
}
