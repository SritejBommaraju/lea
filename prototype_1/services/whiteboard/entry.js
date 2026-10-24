// Bundles VEL's whiteboard renderer (VEL's v2/stage.js, read only) for Lea's WKWebView.
// Build: VEL_DIR=/path/to/vel npm run build   (writes ../../MyApp/Whiteboard/vel-stage.js)
import { Stage } from "vel/v2/stage.js";
window.VelStage = Stage;
