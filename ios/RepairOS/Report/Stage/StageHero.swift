import SwiftUI

/// What the dark stage shows. `.automatic` picks the 3D view or the photo hero from the report's model source;
/// `.custom` hosts any other hero with the same markers and focus (none is wired in since the photo-real
/// recreation was removed, 2026-10-28):
///
///     ReportStageView(..., hero: .custom { ctx in AnyView(MyHero(markers: ctx.markers, focus: ctx.focus,
///                                                              onMarkerTap: ctx.select, onLoaded: ctx.ready,
///                                                              probe: ctx.probe, tour: ctx.tour, cue: ctx.cue)) })
enum StageHero {
    case automatic
    case custom((StageHeroContext) -> AnyView)
}

/// Everything a hero needs from the report: the numbered markers (plus Lea's own rings on parts that are not
/// problems, which carry a `ringRadius`), the focused one, and what to call when one is tapped.
struct StageHeroContext {
    let markers: [ModelPin]
    let focus: String?
    let select: (String) -> Void
    /// Call once the hero is drawn: the landing (markers, verdict) waits for it, at most 2.5 s.
    let ready: () -> Void
    /// Pass to the hero: it reports where the camera has landed (the proof photo hand-off).
    let probe: StageProbe
    /// The walkthrough while it plays (nil otherwise): pass it on, so its pager drives the hero's camera.
    var tour: WalkthroughPlayer? = nil
    /// Lea's latest camera move while she answers (LeaStage): `.show(marker id)`, `.view(side)` or `.zoom`.
    var cue: LeaStageCue? = nil
}
