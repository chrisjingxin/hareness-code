import { createRequire } from "node:module";
var __create = Object.create;
var __getProtoOf = Object.getPrototypeOf;
var __defProp = Object.defineProperty;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
function __accessProp(key) {
  return this[key];
}
var __toESMCache_node;
var __toESMCache_esm;
var __toESM = (mod, isNodeMode, target) => {
  var canCache = mod != null && typeof mod === "object";
  if (canCache) {
    var cache = isNodeMode ? __toESMCache_node ??= new WeakMap : __toESMCache_esm ??= new WeakMap;
    var cached = cache.get(mod);
    if (cached)
      return cached;
  }
  target = mod != null ? __create(__getProtoOf(mod)) : {};
  const to = isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target;
  for (let key of __getOwnPropNames(mod))
    if (!__hasOwnProp.call(to, key))
      __defProp(to, key, {
        get: __accessProp.bind(mod, key),
        enumerable: true
      });
  if (canCache)
    cache.set(mod, to);
  return to;
};
var __commonJS = (cb, mod) => () => (mod || cb((mod = { exports: {} }).exports, mod), mod.exports);
var __require = /* @__PURE__ */ createRequire(import.meta.url);

// ../../node_modules/.bun/emoji-regex@10.6.0/node_modules/emoji-regex/index.js
var require_emoji_regex = __commonJS((exports, module) => {
  module.exports = () => {
    return /[#*0-9]\uFE0F?\u20E3|[\xA9\xAE\u203C\u2049\u2122\u2139\u2194-\u2199\u21A9\u21AA\u231A\u231B\u2328\u23CF\u23ED-\u23EF\u23F1\u23F2\u23F8-\u23FA\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB\u25FC\u25FE\u2600-\u2604\u260E\u2611\u2614\u2615\u2618\u2620\u2622\u2623\u2626\u262A\u262E\u262F\u2638-\u263A\u2640\u2642\u2648-\u2653\u265F\u2660\u2663\u2665\u2666\u2668\u267B\u267E\u267F\u2692\u2694-\u2697\u2699\u269B\u269C\u26A0\u26A7\u26AA\u26B0\u26B1\u26BD\u26BE\u26C4\u26C8\u26CF\u26D1\u26E9\u26F0-\u26F5\u26F7\u26F8\u26FA\u2702\u2708\u2709\u270F\u2712\u2714\u2716\u271D\u2721\u2733\u2734\u2744\u2747\u2757\u2763\u27A1\u2934\u2935\u2B05-\u2B07\u2B1B\u2B1C\u2B55\u3030\u303D\u3297\u3299]\uFE0F?|[\u261D\u270C\u270D](?:\uD83C[\uDFFB-\uDFFF]|\uFE0F)?|[\u270A\u270B](?:\uD83C[\uDFFB-\uDFFF])?|[\u23E9-\u23EC\u23F0\u23F3\u25FD\u2693\u26A1\u26AB\u26C5\u26CE\u26D4\u26EA\u26FD\u2705\u2728\u274C\u274E\u2753-\u2755\u2795-\u2797\u27B0\u27BF\u2B50]|\u26D3\uFE0F?(?:\u200D\uD83D\uDCA5)?|\u26F9(?:\uD83C[\uDFFB-\uDFFF]|\uFE0F)?(?:\u200D[\u2640\u2642]\uFE0F?)?|\u2764\uFE0F?(?:\u200D(?:\uD83D\uDD25|\uD83E\uDE79))?|\uD83C(?:[\uDC04\uDD70\uDD71\uDD7E\uDD7F\uDE02\uDE37\uDF21\uDF24-\uDF2C\uDF36\uDF7D\uDF96\uDF97\uDF99-\uDF9B\uDF9E\uDF9F\uDFCD\uDFCE\uDFD4-\uDFDF\uDFF5\uDFF7]\uFE0F?|[\uDF85\uDFC2\uDFC7](?:\uD83C[\uDFFB-\uDFFF])?|[\uDFC4\uDFCA](?:\uD83C[\uDFFB-\uDFFF])?(?:\u200D[\u2640\u2642]\uFE0F?)?|[\uDFCB\uDFCC](?:\uD83C[\uDFFB-\uDFFF]|\uFE0F)?(?:\u200D[\u2640\u2642]\uFE0F?)?|[\uDCCF\uDD8E\uDD91-\uDD9A\uDE01\uDE1A\uDE2F\uDE32-\uDE36\uDE38-\uDE3A\uDE50\uDE51\uDF00-\uDF20\uDF2D-\uDF35\uDF37-\uDF43\uDF45-\uDF4A\uDF4C-\uDF7C\uDF7E-\uDF84\uDF86-\uDF93\uDFA0-\uDFC1\uDFC5\uDFC6\uDFC8\uDFC9\uDFCF-\uDFD3\uDFE0-\uDFF0\uDFF8-\uDFFF]|\uDDE6\uD83C[\uDDE8-\uDDEC\uDDEE\uDDF1\uDDF2\uDDF4\uDDF6-\uDDFA\uDDFC\uDDFD\uDDFF]|\uDDE7\uD83C[\uDDE6\uDDE7\uDDE9-\uDDEF\uDDF1-\uDDF4\uDDF6-\uDDF9\uDDFB\uDDFC\uDDFE\uDDFF]|\uDDE8\uD83C[\uDDE6\uDDE8\uDDE9\uDDEB-\uDDEE\uDDF0-\uDDF7\uDDFA-\uDDFF]|\uDDE9\uD83C[\uDDEA\uDDEC\uDDEF\uDDF0\uDDF2\uDDF4\uDDFF]|\uDDEA\uD83C[\uDDE6\uDDE8\uDDEA\uDDEC\uDDED\uDDF7-\uDDFA]|\uDDEB\uD83C[\uDDEE-\uDDF0\uDDF2\uDDF4\uDDF7]|\uDDEC\uD83C[\uDDE6\uDDE7\uDDE9-\uDDEE\uDDF1-\uDDF3\uDDF5-\uDDFA\uDDFC\uDDFE]|\uDDED\uD83C[\uDDF0\uDDF2\uDDF3\uDDF7\uDDF9\uDDFA]|\uDDEE\uD83C[\uDDE8-\uDDEA\uDDF1-\uDDF4\uDDF6-\uDDF9]|\uDDEF\uD83C[\uDDEA\uDDF2\uDDF4\uDDF5]|\uDDF0\uD83C[\uDDEA\uDDEC-\uDDEE\uDDF2\uDDF3\uDDF5\uDDF7\uDDFC\uDDFE\uDDFF]|\uDDF1\uD83C[\uDDE6-\uDDE8\uDDEE\uDDF0\uDDF7-\uDDFB\uDDFE]|\uDDF2\uD83C[\uDDE6\uDDE8-\uDDED\uDDF0-\uDDFF]|\uDDF3\uD83C[\uDDE6\uDDE8\uDDEA-\uDDEC\uDDEE\uDDF1\uDDF4\uDDF5\uDDF7\uDDFA\uDDFF]|\uDDF4\uD83C\uDDF2|\uDDF5\uD83C[\uDDE6\uDDEA-\uDDED\uDDF0-\uDDF3\uDDF7-\uDDF9\uDDFC\uDDFE]|\uDDF6\uD83C\uDDE6|\uDDF7\uD83C[\uDDEA\uDDF4\uDDF8\uDDFA\uDDFC]|\uDDF8\uD83C[\uDDE6-\uDDEA\uDDEC-\uDDF4\uDDF7-\uDDF9\uDDFB\uDDFD-\uDDFF]|\uDDF9\uD83C[\uDDE6\uDDE8\uDDE9\uDDEB-\uDDED\uDDEF-\uDDF4\uDDF7\uDDF9\uDDFB\uDDFC\uDDFF]|\uDDFA\uD83C[\uDDE6\uDDEC\uDDF2\uDDF3\uDDF8\uDDFE\uDDFF]|\uDDFB\uD83C[\uDDE6\uDDE8\uDDEA\uDDEC\uDDEE\uDDF3\uDDFA]|\uDDFC\uD83C[\uDDEB\uDDF8]|\uDDFD\uD83C\uDDF0|\uDDFE\uD83C[\uDDEA\uDDF9]|\uDDFF\uD83C[\uDDE6\uDDF2\uDDFC]|\uDF44(?:\u200D\uD83D\uDFEB)?|\uDF4B(?:\u200D\uD83D\uDFE9)?|\uDFC3(?:\uD83C[\uDFFB-\uDFFF])?(?:\u200D(?:[\u2640\u2642]\uFE0F?(?:\u200D\u27A1\uFE0F?)?|\u27A1\uFE0F?))?|\uDFF3\uFE0F?(?:\u200D(?:\u26A7\uFE0F?|\uD83C\uDF08))?|\uDFF4(?:\u200D\u2620\uFE0F?|\uDB40\uDC67\uDB40\uDC62\uDB40(?:\uDC65\uDB40\uDC6E\uDB40\uDC67|\uDC73\uDB40\uDC63\uDB40\uDC74|\uDC77\uDB40\uDC6C\uDB40\uDC73)\uDB40\uDC7F)?)|\uD83D(?:[\uDC3F\uDCFD\uDD49\uDD4A\uDD6F\uDD70\uDD73\uDD76-\uDD79\uDD87\uDD8A-\uDD8D\uDDA5\uDDA8\uDDB1\uDDB2\uDDBC\uDDC2-\uDDC4\uDDD1-\uDDD3\uDDDC-\uDDDE\uDDE1\uDDE3\uDDE8\uDDEF\uDDF3\uDDFA\uDECB\uDECD-\uDECF\uDEE0-\uDEE5\uDEE9\uDEF0\uDEF3]\uFE0F?|[\uDC42\uDC43\uDC46-\uDC50\uDC66\uDC67\uDC6B-\uDC6D\uDC72\uDC74-\uDC76\uDC78\uDC7C\uDC83\uDC85\uDC8F\uDC91\uDCAA\uDD7A\uDD95\uDD96\uDE4C\uDE4F\uDEC0\uDECC](?:\uD83C[\uDFFB-\uDFFF])?|[\uDC6E-\uDC71\uDC73\uDC77\uDC81\uDC82\uDC86\uDC87\uDE45-\uDE47\uDE4B\uDE4D\uDE4E\uDEA3\uDEB4\uDEB5](?:\uD83C[\uDFFB-\uDFFF])?(?:\u200D[\u2640\u2642]\uFE0F?)?|[\uDD74\uDD90](?:\uD83C[\uDFFB-\uDFFF]|\uFE0F)?|[\uDC00-\uDC07\uDC09-\uDC14\uDC16-\uDC25\uDC27-\uDC3A\uDC3C-\uDC3E\uDC40\uDC44\uDC45\uDC51-\uDC65\uDC6A\uDC79-\uDC7B\uDC7D-\uDC80\uDC84\uDC88-\uDC8E\uDC90\uDC92-\uDCA9\uDCAB-\uDCFC\uDCFF-\uDD3D\uDD4B-\uDD4E\uDD50-\uDD67\uDDA4\uDDFB-\uDE2D\uDE2F-\uDE34\uDE37-\uDE41\uDE43\uDE44\uDE48-\uDE4A\uDE80-\uDEA2\uDEA4-\uDEB3\uDEB7-\uDEBF\uDEC1-\uDEC5\uDED0-\uDED2\uDED5-\uDED8\uDEDC-\uDEDF\uDEEB\uDEEC\uDEF4-\uDEFC\uDFE0-\uDFEB\uDFF0]|\uDC08(?:\u200D\u2B1B)?|\uDC15(?:\u200D\uD83E\uDDBA)?|\uDC26(?:\u200D(?:\u2B1B|\uD83D\uDD25))?|\uDC3B(?:\u200D\u2744\uFE0F?)?|\uDC41\uFE0F?(?:\u200D\uD83D\uDDE8\uFE0F?)?|\uDC68(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:\uDC8B\u200D\uD83D)?\uDC68|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDC68\uDC69]\u200D\uD83D(?:\uDC66(?:\u200D\uD83D\uDC66)?|\uDC67(?:\u200D\uD83D[\uDC66\uDC67])?)|[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC66(?:\u200D\uD83D\uDC66)?|\uDC67(?:\u200D\uD83D[\uDC66\uDC67])?)|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3]))|\uD83C(?:\uDFFB(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:\uDC8B\u200D\uD83D)?\uDC68\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC68\uD83C[\uDFFC-\uDFFF])|\uD83E(?:[\uDD1D\uDEEF]\u200D\uD83D\uDC68\uD83C[\uDFFC-\uDFFF]|[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3])))?|\uDFFC(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:\uDC8B\u200D\uD83D)?\uDC68\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC68\uD83C[\uDFFB\uDFFD-\uDFFF])|\uD83E(?:[\uDD1D\uDEEF]\u200D\uD83D\uDC68\uD83C[\uDFFB\uDFFD-\uDFFF]|[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3])))?|\uDFFD(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:\uDC8B\u200D\uD83D)?\uDC68\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC68\uD83C[\uDFFB\uDFFC\uDFFE\uDFFF])|\uD83E(?:[\uDD1D\uDEEF]\u200D\uD83D\uDC68\uD83C[\uDFFB\uDFFC\uDFFE\uDFFF]|[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3])))?|\uDFFE(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:\uDC8B\u200D\uD83D)?\uDC68\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC68\uD83C[\uDFFB-\uDFFD\uDFFF])|\uD83E(?:[\uDD1D\uDEEF]\u200D\uD83D\uDC68\uD83C[\uDFFB-\uDFFD\uDFFF]|[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3])))?|\uDFFF(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:\uDC8B\u200D\uD83D)?\uDC68\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC68\uD83C[\uDFFB-\uDFFE])|\uD83E(?:[\uDD1D\uDEEF]\u200D\uD83D\uDC68\uD83C[\uDFFB-\uDFFE]|[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3])))?))?|\uDC69(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:\uDC8B\u200D\uD83D)?[\uDC68\uDC69]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC66(?:\u200D\uD83D\uDC66)?|\uDC67(?:\u200D\uD83D[\uDC66\uDC67])?|\uDC69\u200D\uD83D(?:\uDC66(?:\u200D\uD83D\uDC66)?|\uDC67(?:\u200D\uD83D[\uDC66\uDC67])?))|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3]))|\uD83C(?:\uDFFB(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:[\uDC68\uDC69]|\uDC8B\u200D\uD83D[\uDC68\uDC69])\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC69\uD83C[\uDFFC-\uDFFF])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3]|\uDD1D\u200D\uD83D[\uDC68\uDC69]\uD83C[\uDFFC-\uDFFF]|\uDEEF\u200D\uD83D\uDC69\uD83C[\uDFFC-\uDFFF])))?|\uDFFC(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:[\uDC68\uDC69]|\uDC8B\u200D\uD83D[\uDC68\uDC69])\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC69\uD83C[\uDFFB\uDFFD-\uDFFF])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3]|\uDD1D\u200D\uD83D[\uDC68\uDC69]\uD83C[\uDFFB\uDFFD-\uDFFF]|\uDEEF\u200D\uD83D\uDC69\uD83C[\uDFFB\uDFFD-\uDFFF])))?|\uDFFD(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:[\uDC68\uDC69]|\uDC8B\u200D\uD83D[\uDC68\uDC69])\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC69\uD83C[\uDFFB\uDFFC\uDFFE\uDFFF])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3]|\uDD1D\u200D\uD83D[\uDC68\uDC69]\uD83C[\uDFFB\uDFFC\uDFFE\uDFFF]|\uDEEF\u200D\uD83D\uDC69\uD83C[\uDFFB\uDFFC\uDFFE\uDFFF])))?|\uDFFE(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:[\uDC68\uDC69]|\uDC8B\u200D\uD83D[\uDC68\uDC69])\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC69\uD83C[\uDFFB-\uDFFD\uDFFF])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3]|\uDD1D\u200D\uD83D[\uDC68\uDC69]\uD83C[\uDFFB-\uDFFD\uDFFF]|\uDEEF\u200D\uD83D\uDC69\uD83C[\uDFFB-\uDFFD\uDFFF])))?|\uDFFF(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D\uD83D(?:[\uDC68\uDC69]|\uDC8B\u200D\uD83D[\uDC68\uDC69])\uD83C[\uDFFB-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83D\uDC69\uD83C[\uDFFB-\uDFFE])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3]|\uDD1D\u200D\uD83D[\uDC68\uDC69]\uD83C[\uDFFB-\uDFFE]|\uDEEF\u200D\uD83D\uDC69\uD83C[\uDFFB-\uDFFE])))?))?|\uDD75(?:\uD83C[\uDFFB-\uDFFF]|\uFE0F)?(?:\u200D[\u2640\u2642]\uFE0F?)?|\uDE2E(?:\u200D\uD83D\uDCA8)?|\uDE35(?:\u200D\uD83D\uDCAB)?|\uDE36(?:\u200D\uD83C\uDF2B\uFE0F?)?|\uDE42(?:\u200D[\u2194\u2195]\uFE0F?)?|\uDEB6(?:\uD83C[\uDFFB-\uDFFF])?(?:\u200D(?:[\u2640\u2642]\uFE0F?(?:\u200D\u27A1\uFE0F?)?|\u27A1\uFE0F?))?)|\uD83E(?:[\uDD0C\uDD0F\uDD18-\uDD1F\uDD30-\uDD34\uDD36\uDD77\uDDB5\uDDB6\uDDBB\uDDD2\uDDD3\uDDD5\uDEC3-\uDEC5\uDEF0\uDEF2-\uDEF8](?:\uD83C[\uDFFB-\uDFFF])?|[\uDD26\uDD35\uDD37-\uDD39\uDD3C-\uDD3E\uDDB8\uDDB9\uDDCD\uDDCF\uDDD4\uDDD6-\uDDDD](?:\uD83C[\uDFFB-\uDFFF])?(?:\u200D[\u2640\u2642]\uFE0F?)?|[\uDDDE\uDDDF](?:\u200D[\u2640\u2642]\uFE0F?)?|[\uDD0D\uDD0E\uDD10-\uDD17\uDD20-\uDD25\uDD27-\uDD2F\uDD3A\uDD3F-\uDD45\uDD47-\uDD76\uDD78-\uDDB4\uDDB7\uDDBA\uDDBC-\uDDCC\uDDD0\uDDE0-\uDDFF\uDE70-\uDE7C\uDE80-\uDE8A\uDE8E-\uDEC2\uDEC6\uDEC8\uDECD-\uDEDC\uDEDF-\uDEEA\uDEEF]|\uDDCE(?:\uD83C[\uDFFB-\uDFFF])?(?:\u200D(?:[\u2640\u2642]\uFE0F?(?:\u200D\u27A1\uFE0F?)?|\u27A1\uFE0F?))?|\uDDD1(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\uD83C[\uDF3E\uDF73\uDF7C\uDF84\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3\uDE70]|\uDD1D\u200D\uD83E\uDDD1|\uDDD1\u200D\uD83E\uDDD2(?:\u200D\uD83E\uDDD2)?|\uDDD2(?:\u200D\uD83E\uDDD2)?))|\uD83C(?:\uDFFB(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D(?:\uD83D\uDC8B\u200D)?\uD83E\uDDD1\uD83C[\uDFFC-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF84\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83E\uDDD1\uD83C[\uDFFC-\uDFFF])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3\uDE70]|\uDD1D\u200D\uD83E\uDDD1\uD83C[\uDFFB-\uDFFF]|\uDEEF\u200D\uD83E\uDDD1\uD83C[\uDFFC-\uDFFF])))?|\uDFFC(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D(?:\uD83D\uDC8B\u200D)?\uD83E\uDDD1\uD83C[\uDFFB\uDFFD-\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF84\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83E\uDDD1\uD83C[\uDFFB\uDFFD-\uDFFF])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3\uDE70]|\uDD1D\u200D\uD83E\uDDD1\uD83C[\uDFFB-\uDFFF]|\uDEEF\u200D\uD83E\uDDD1\uD83C[\uDFFB\uDFFD-\uDFFF])))?|\uDFFD(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D(?:\uD83D\uDC8B\u200D)?\uD83E\uDDD1\uD83C[\uDFFB\uDFFC\uDFFE\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF84\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83E\uDDD1\uD83C[\uDFFB\uDFFC\uDFFE\uDFFF])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3\uDE70]|\uDD1D\u200D\uD83E\uDDD1\uD83C[\uDFFB-\uDFFF]|\uDEEF\u200D\uD83E\uDDD1\uD83C[\uDFFB\uDFFC\uDFFE\uDFFF])))?|\uDFFE(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D(?:\uD83D\uDC8B\u200D)?\uD83E\uDDD1\uD83C[\uDFFB-\uDFFD\uDFFF]|\uD83C[\uDF3E\uDF73\uDF7C\uDF84\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83E\uDDD1\uD83C[\uDFFB-\uDFFD\uDFFF])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3\uDE70]|\uDD1D\u200D\uD83E\uDDD1\uD83C[\uDFFB-\uDFFF]|\uDEEF\u200D\uD83E\uDDD1\uD83C[\uDFFB-\uDFFD\uDFFF])))?|\uDFFF(?:\u200D(?:[\u2695\u2696\u2708]\uFE0F?|\u2764\uFE0F?\u200D(?:\uD83D\uDC8B\u200D)?\uD83E\uDDD1\uD83C[\uDFFB-\uDFFE]|\uD83C[\uDF3E\uDF73\uDF7C\uDF84\uDF93\uDFA4\uDFA8\uDFEB\uDFED]|\uD83D(?:[\uDCBB\uDCBC\uDD27\uDD2C\uDE80\uDE92]|\uDC30\u200D\uD83E\uDDD1\uD83C[\uDFFB-\uDFFE])|\uD83E(?:[\uDDAF\uDDBC\uDDBD](?:\u200D\u27A1\uFE0F?)?|[\uDDB0-\uDDB3\uDE70]|\uDD1D\u200D\uD83E\uDDD1\uD83C[\uDFFB-\uDFFF]|\uDEEF\u200D\uD83E\uDDD1\uD83C[\uDFFB-\uDFFE])))?))?|\uDEF1(?:\uD83C(?:\uDFFB(?:\u200D\uD83E\uDEF2\uD83C[\uDFFC-\uDFFF])?|\uDFFC(?:\u200D\uD83E\uDEF2\uD83C[\uDFFB\uDFFD-\uDFFF])?|\uDFFD(?:\u200D\uD83E\uDEF2\uD83C[\uDFFB\uDFFC\uDFFE\uDFFF])?|\uDFFE(?:\u200D\uD83E\uDEF2\uD83C[\uDFFB-\uDFFD\uDFFF])?|\uDFFF(?:\u200D\uD83E\uDEF2\uD83C[\uDFFB-\uDFFE])?))?)/g;
  };
});

// src/lib/tree-sitter/parser.worker.ts
import { Parser, Query, Language } from "web-tree-sitter";
import { mkdir as mkdir3 } from "fs/promises";
import * as path2 from "path";

// src/lib/tree-sitter/download-utils.ts
import { mkdir, readFile, writeFile } from "fs/promises";
import * as path from "path";

class DownloadUtils {
  static hashUrl(url) {
    let hash = 0;
    for (let i = 0;i < url.length; i++) {
      const char = url.charCodeAt(i);
      hash = (hash << 5) - hash + char;
      hash = hash & hash;
    }
    return Math.abs(hash).toString(16);
  }
  static async downloadOrLoad(source, cacheDir, cacheSubdir, fileExtension, useHashForCache = true, filetype) {
    const isUrl = source.startsWith("http://") || source.startsWith("https://");
    if (isUrl) {
      let cacheFileName;
      if (useHashForCache) {
        const hash = this.hashUrl(source);
        cacheFileName = filetype ? `${filetype}-${hash}${fileExtension}` : `${hash}${fileExtension}`;
      } else {
        cacheFileName = path.basename(source);
      }
      const cacheFile = path.join(cacheDir, cacheSubdir, cacheFileName);
      await mkdir(path.dirname(cacheFile), { recursive: true });
      try {
        const cachedContent = await readFile(cacheFile);
        if (cachedContent.byteLength > 0) {
          console.log(`Loaded from cache: ${cacheFile} (${source})`);
          return { content: cachedContent, filePath: cacheFile };
        }
      } catch (error) {}
      try {
        console.log(`Downloading from URL: ${source}`);
        const response = await fetch(source);
        if (!response.ok) {
          return { error: `Failed to fetch from ${source}: ${response.statusText}` };
        }
        const content = Buffer.from(await response.arrayBuffer());
        try {
          await writeFile(cacheFile, Buffer.from(content));
          console.log(`Cached: ${source}`);
        } catch (cacheError) {
          console.warn(`Failed to cache: ${cacheError}`);
        }
        return { content, filePath: cacheFile };
      } catch (error) {
        return { error: `Error downloading from ${source}: ${error}` };
      }
    } else {
      try {
        console.log(`Loading from local path: ${source}`);
        const content = await readFile(source);
        return { content, filePath: source };
      } catch (error) {
        return { error: `Error loading from local path ${source}: ${error}` };
      }
    }
  }
  static async downloadToPath(source, targetPath) {
    const isUrl = source.startsWith("http://") || source.startsWith("https://");
    await mkdir(path.dirname(targetPath), { recursive: true });
    if (isUrl) {
      try {
        console.log(`Downloading from URL: ${source}`);
        const response = await fetch(source);
        if (!response.ok) {
          return { error: `Failed to fetch from ${source}: ${response.statusText}` };
        }
        const content = Buffer.from(await response.arrayBuffer());
        await writeFile(targetPath, Buffer.from(content));
        console.log(`Downloaded: ${source} -> ${targetPath}`);
        return { content, filePath: targetPath };
      } catch (error) {
        return { error: `Error downloading from ${source}: ${error}` };
      }
    } else {
      try {
        console.log(`Copying from local path: ${source}`);
        const content = await readFile(source);
        await writeFile(targetPath, Buffer.from(content));
        return { content, filePath: targetPath };
      } catch (error) {
        return { error: `Error copying from local path ${source}: ${error}` };
      }
    }
  }
  static async fetchHighlightQueries(sources, cacheDir, filetype) {
    const queryPromises = sources.map((source) => this.fetchHighlightQuery(source, cacheDir, filetype));
    const queryResults = await Promise.all(queryPromises);
    const validQueries = queryResults.filter((query) => query.trim().length > 0);
    return validQueries.join(`
`);
  }
  static async fetchHighlightQuery(source, cacheDir, filetype) {
    const result = await this.downloadOrLoad(source, cacheDir, "queries", ".scm", true, filetype);
    if (result.error) {
      console.error(`Error fetching highlight query from ${source}:`, result.error);
      return "";
    }
    if (result.content) {
      return new TextDecoder().decode(result.content);
    }
    return "";
  }
}

// src/lib/bunfs.ts
import { basename as basename2, join as join2 } from "node:path";
function isBunfsPath(path2) {
  return path2.includes("$bunfs") || /^B:[\\/]~BUN/i.test(path2);
}
function getBunfsRootPath() {
  return process.platform === "win32" ? "B:\\~BUN\\root" : "/$bunfs/root";
}
function normalizeBunfsPath(fileName) {
  return join2(getBunfsRootPath(), basename2(fileName));
}

// src/platform/runtime.ts
import { existsSync } from "node:fs";
import { mkdir as mkdir2, writeFile as writeFileNode } from "node:fs/promises";
import { dirname as dirname2, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// ../../node_modules/.bun/ansi-regex@6.2.2/node_modules/ansi-regex/index.js
function ansiRegex({ onlyFirst = false } = {}) {
  const ST = "(?:\\u0007|\\u001B\\u005C|\\u009C)";
  const osc = `(?:\\u001B\\][\\s\\S]*?${ST})`;
  const csi = "[\\u001B\\u009B][[\\]()#;?]*(?:\\d{1,4}(?:[;:]\\d{0,4})*)?[\\dA-PR-TZcf-nq-uy=><~]";
  const pattern = `${osc}|${csi}`;
  return new RegExp(pattern, onlyFirst ? undefined : "g");
}

// ../../node_modules/.bun/strip-ansi@7.1.2/node_modules/strip-ansi/index.js
var regex = ansiRegex();
function stripAnsi(string) {
  if (typeof string !== "string") {
    throw new TypeError(`Expected a \`string\`, got \`${typeof string}\``);
  }
  return string.replace(regex, "");
}

// ../../node_modules/.bun/get-east-asian-width@1.4.0/node_modules/get-east-asian-width/lookup.js
function isAmbiguous(x) {
  return x === 161 || x === 164 || x === 167 || x === 168 || x === 170 || x === 173 || x === 174 || x >= 176 && x <= 180 || x >= 182 && x <= 186 || x >= 188 && x <= 191 || x === 198 || x === 208 || x === 215 || x === 216 || x >= 222 && x <= 225 || x === 230 || x >= 232 && x <= 234 || x === 236 || x === 237 || x === 240 || x === 242 || x === 243 || x >= 247 && x <= 250 || x === 252 || x === 254 || x === 257 || x === 273 || x === 275 || x === 283 || x === 294 || x === 295 || x === 299 || x >= 305 && x <= 307 || x === 312 || x >= 319 && x <= 322 || x === 324 || x >= 328 && x <= 331 || x === 333 || x === 338 || x === 339 || x === 358 || x === 359 || x === 363 || x === 462 || x === 464 || x === 466 || x === 468 || x === 470 || x === 472 || x === 474 || x === 476 || x === 593 || x === 609 || x === 708 || x === 711 || x >= 713 && x <= 715 || x === 717 || x === 720 || x >= 728 && x <= 731 || x === 733 || x === 735 || x >= 768 && x <= 879 || x >= 913 && x <= 929 || x >= 931 && x <= 937 || x >= 945 && x <= 961 || x >= 963 && x <= 969 || x === 1025 || x >= 1040 && x <= 1103 || x === 1105 || x === 8208 || x >= 8211 && x <= 8214 || x === 8216 || x === 8217 || x === 8220 || x === 8221 || x >= 8224 && x <= 8226 || x >= 8228 && x <= 8231 || x === 8240 || x === 8242 || x === 8243 || x === 8245 || x === 8251 || x === 8254 || x === 8308 || x === 8319 || x >= 8321 && x <= 8324 || x === 8364 || x === 8451 || x === 8453 || x === 8457 || x === 8467 || x === 8470 || x === 8481 || x === 8482 || x === 8486 || x === 8491 || x === 8531 || x === 8532 || x >= 8539 && x <= 8542 || x >= 8544 && x <= 8555 || x >= 8560 && x <= 8569 || x === 8585 || x >= 8592 && x <= 8601 || x === 8632 || x === 8633 || x === 8658 || x === 8660 || x === 8679 || x === 8704 || x === 8706 || x === 8707 || x === 8711 || x === 8712 || x === 8715 || x === 8719 || x === 8721 || x === 8725 || x === 8730 || x >= 8733 && x <= 8736 || x === 8739 || x === 8741 || x >= 8743 && x <= 8748 || x === 8750 || x >= 8756 && x <= 8759 || x === 8764 || x === 8765 || x === 8776 || x === 8780 || x === 8786 || x === 8800 || x === 8801 || x >= 8804 && x <= 8807 || x === 8810 || x === 8811 || x === 8814 || x === 8815 || x === 8834 || x === 8835 || x === 8838 || x === 8839 || x === 8853 || x === 8857 || x === 8869 || x === 8895 || x === 8978 || x >= 9312 && x <= 9449 || x >= 9451 && x <= 9547 || x >= 9552 && x <= 9587 || x >= 9600 && x <= 9615 || x >= 9618 && x <= 9621 || x === 9632 || x === 9633 || x >= 9635 && x <= 9641 || x === 9650 || x === 9651 || x === 9654 || x === 9655 || x === 9660 || x === 9661 || x === 9664 || x === 9665 || x >= 9670 && x <= 9672 || x === 9675 || x >= 9678 && x <= 9681 || x >= 9698 && x <= 9701 || x === 9711 || x === 9733 || x === 9734 || x === 9737 || x === 9742 || x === 9743 || x === 9756 || x === 9758 || x === 9792 || x === 9794 || x === 9824 || x === 9825 || x >= 9827 && x <= 9829 || x >= 9831 && x <= 9834 || x === 9836 || x === 9837 || x === 9839 || x === 9886 || x === 9887 || x === 9919 || x >= 9926 && x <= 9933 || x >= 9935 && x <= 9939 || x >= 9941 && x <= 9953 || x === 9955 || x === 9960 || x === 9961 || x >= 9963 && x <= 9969 || x === 9972 || x >= 9974 && x <= 9977 || x === 9979 || x === 9980 || x === 9982 || x === 9983 || x === 10045 || x >= 10102 && x <= 10111 || x >= 11094 && x <= 11097 || x >= 12872 && x <= 12879 || x >= 57344 && x <= 63743 || x >= 65024 && x <= 65039 || x === 65533 || x >= 127232 && x <= 127242 || x >= 127248 && x <= 127277 || x >= 127280 && x <= 127337 || x >= 127344 && x <= 127373 || x === 127375 || x === 127376 || x >= 127387 && x <= 127404 || x >= 917760 && x <= 917999 || x >= 983040 && x <= 1048573 || x >= 1048576 && x <= 1114109;
}
function isFullWidth(x) {
  return x === 12288 || x >= 65281 && x <= 65376 || x >= 65504 && x <= 65510;
}
function isWide(x) {
  return x >= 4352 && x <= 4447 || x === 8986 || x === 8987 || x === 9001 || x === 9002 || x >= 9193 && x <= 9196 || x === 9200 || x === 9203 || x === 9725 || x === 9726 || x === 9748 || x === 9749 || x >= 9776 && x <= 9783 || x >= 9800 && x <= 9811 || x === 9855 || x >= 9866 && x <= 9871 || x === 9875 || x === 9889 || x === 9898 || x === 9899 || x === 9917 || x === 9918 || x === 9924 || x === 9925 || x === 9934 || x === 9940 || x === 9962 || x === 9970 || x === 9971 || x === 9973 || x === 9978 || x === 9981 || x === 9989 || x === 9994 || x === 9995 || x === 10024 || x === 10060 || x === 10062 || x >= 10067 && x <= 10069 || x === 10071 || x >= 10133 && x <= 10135 || x === 10160 || x === 10175 || x === 11035 || x === 11036 || x === 11088 || x === 11093 || x >= 11904 && x <= 11929 || x >= 11931 && x <= 12019 || x >= 12032 && x <= 12245 || x >= 12272 && x <= 12287 || x >= 12289 && x <= 12350 || x >= 12353 && x <= 12438 || x >= 12441 && x <= 12543 || x >= 12549 && x <= 12591 || x >= 12593 && x <= 12686 || x >= 12688 && x <= 12773 || x >= 12783 && x <= 12830 || x >= 12832 && x <= 12871 || x >= 12880 && x <= 42124 || x >= 42128 && x <= 42182 || x >= 43360 && x <= 43388 || x >= 44032 && x <= 55203 || x >= 63744 && x <= 64255 || x >= 65040 && x <= 65049 || x >= 65072 && x <= 65106 || x >= 65108 && x <= 65126 || x >= 65128 && x <= 65131 || x >= 94176 && x <= 94180 || x >= 94192 && x <= 94198 || x >= 94208 && x <= 101589 || x >= 101631 && x <= 101662 || x >= 101760 && x <= 101874 || x >= 110576 && x <= 110579 || x >= 110581 && x <= 110587 || x === 110589 || x === 110590 || x >= 110592 && x <= 110882 || x === 110898 || x >= 110928 && x <= 110930 || x === 110933 || x >= 110948 && x <= 110951 || x >= 110960 && x <= 111355 || x >= 119552 && x <= 119638 || x >= 119648 && x <= 119670 || x === 126980 || x === 127183 || x === 127374 || x >= 127377 && x <= 127386 || x >= 127488 && x <= 127490 || x >= 127504 && x <= 127547 || x >= 127552 && x <= 127560 || x === 127568 || x === 127569 || x >= 127584 && x <= 127589 || x >= 127744 && x <= 127776 || x >= 127789 && x <= 127797 || x >= 127799 && x <= 127868 || x >= 127870 && x <= 127891 || x >= 127904 && x <= 127946 || x >= 127951 && x <= 127955 || x >= 127968 && x <= 127984 || x === 127988 || x >= 127992 && x <= 128062 || x === 128064 || x >= 128066 && x <= 128252 || x >= 128255 && x <= 128317 || x >= 128331 && x <= 128334 || x >= 128336 && x <= 128359 || x === 128378 || x === 128405 || x === 128406 || x === 128420 || x >= 128507 && x <= 128591 || x >= 128640 && x <= 128709 || x === 128716 || x >= 128720 && x <= 128722 || x >= 128725 && x <= 128728 || x >= 128732 && x <= 128735 || x === 128747 || x === 128748 || x >= 128756 && x <= 128764 || x >= 128992 && x <= 129003 || x === 129008 || x >= 129292 && x <= 129338 || x >= 129340 && x <= 129349 || x >= 129351 && x <= 129535 || x >= 129648 && x <= 129660 || x >= 129664 && x <= 129674 || x >= 129678 && x <= 129734 || x === 129736 || x >= 129741 && x <= 129756 || x >= 129759 && x <= 129770 || x >= 129775 && x <= 129784 || x >= 131072 && x <= 196605 || x >= 196608 && x <= 262141;
}

// ../../node_modules/.bun/get-east-asian-width@1.4.0/node_modules/get-east-asian-width/index.js
function validate(codePoint) {
  if (!Number.isSafeInteger(codePoint)) {
    throw new TypeError(`Expected a code point, got \`${typeof codePoint}\`.`);
  }
}
function eastAsianWidth(codePoint, { ambiguousAsWide = false } = {}) {
  validate(codePoint);
  if (isFullWidth(codePoint) || isWide(codePoint) || ambiguousAsWide && isAmbiguous(codePoint)) {
    return 2;
  }
  return 1;
}

// ../../node_modules/.bun/string-width@7.2.0/node_modules/string-width/index.js
var import_emoji_regex = __toESM(require_emoji_regex(), 1);
var segmenter = new Intl.Segmenter;
var defaultIgnorableCodePointRegex = /^\p{Default_Ignorable_Code_Point}$/u;
function stringWidth(string, options = {}) {
  if (typeof string !== "string" || string.length === 0) {
    return 0;
  }
  const {
    ambiguousIsNarrow = true,
    countAnsiEscapeCodes = false
  } = options;
  if (!countAnsiEscapeCodes) {
    string = stripAnsi(string);
  }
  if (string.length === 0) {
    return 0;
  }
  let width = 0;
  const eastAsianWidthOptions = { ambiguousAsWide: !ambiguousIsNarrow };
  for (const { segment: character } of segmenter.segment(string)) {
    const codePoint = character.codePointAt(0);
    if (codePoint <= 31 || codePoint >= 127 && codePoint <= 159) {
      continue;
    }
    if (codePoint >= 8203 && codePoint <= 8207 || codePoint === 65279) {
      continue;
    }
    if (codePoint >= 768 && codePoint <= 879 || codePoint >= 6832 && codePoint <= 6911 || codePoint >= 7616 && codePoint <= 7679 || codePoint >= 8400 && codePoint <= 8447 || codePoint >= 65056 && codePoint <= 65071) {
      continue;
    }
    if (codePoint >= 55296 && codePoint <= 57343) {
      continue;
    }
    if (codePoint >= 65024 && codePoint <= 65039) {
      continue;
    }
    if (defaultIgnorableCodePointRegex.test(character)) {
      continue;
    }
    if (import_emoji_regex.default().test(character)) {
      width += 2;
      continue;
    }
    width += eastAsianWidth(codePoint, eastAsianWidthOptions);
  }
  return width;
}

// src/platform/runtime.ts
var TEXT_ENCODER = new TextEncoder;
var bun = globalThis.Bun;
var sleep = bun?.sleep ?? standardSleep;
var stringWidth2 = bun?.stringWidth ?? stringWidth;
var stripANSI = bun?.stripANSI ?? stripAnsi;
var writeFile2 = bun?.write ?? writeFilePortable;
async function resolveBundledFilePath(loadBundledFile, fallbackPath, metaUrl) {
  if (!bun) {
    const path2 = resolveFallbackFilePath(fallbackPath, metaUrl);
    if (existsSync(path2)) {
      return path2;
    }
    return await loadBundledFilePath(loadBundledFile, metaUrl) ?? path2;
  }
  return normalizeLoadedFilePath((await loadBundledFile()).default, metaUrl);
}
function resolveFallbackFilePath(fallbackPath, metaUrl) {
  const path2 = typeof fallbackPath === "function" ? fallbackPath() : fallbackPath;
  return fileURLToPath(path2 instanceof URL ? path2 : new URL(path2, metaUrl));
}
function normalizeLoadedFilePath(loadedPath, baseUrl) {
  if (loadedPath.startsWith("file:")) {
    return fileURLToPath(loadedPath);
  }
  if (isAbsolute(loadedPath)) {
    return loadedPath;
  }
  return resolve(dirname2(fileURLToPath(baseUrl)), loadedPath);
}
async function loadBundledFilePath(loadBundledFile, metaUrl) {
  const specifier = extractBundledImportSpecifier(loadBundledFile);
  if (!specifier) {
    return;
  }
  try {
    const moduleUrl = new URL(specifier, metaUrl);
    const loaded = await import(moduleUrl.href);
    return normalizeLoadedFilePath(loaded.default, moduleUrl.href);
  } catch {
    return;
  }
}
function extractBundledImportSpecifier(loadBundledFile) {
  const match = String(loadBundledFile).match(/\bimport\(\s*(["'`])([^"'`]+)\1/);
  return match?.[2];
}
function standardSleep(msOrDate) {
  const ms = msOrDate instanceof Date ? msOrDate.getTime() - Date.now() : msOrDate;
  return new Promise((resolve2) => setTimeout(resolve2, ms));
}
async function writeFilePortable(destination, data, options) {
  const destinationPath = destination instanceof URL ? fileURLToPath(destination) : destination;
  if (options?.createPath) {
    await mkdir2(dirname2(destinationPath), { recursive: true });
  }
  const bytes = typeof data === "string" ? TEXT_ENCODER.encode(data) : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  await writeFileNode(destinationPath, bytes, { mode: options?.mode });
  return bytes.byteLength;
}

// src/platform/worker.ts
var WORKER_UNAVAILABLE = "OpenTUI tree-sitter workers are not available for this runtime yet.";
var globalWithWorker = globalThis;
var nodeWorkerThreads = getBuiltinModule("node:worker_threads");
var runtimeBridge = loadWorkerRuntime(nodeWorkerThreads);

class UnsupportedWorker {
  onmessage = null;
  onerror = null;
  constructor() {
    throw new Error(WORKER_UNAVAILABLE);
  }
  postMessage() {
    throw new Error(WORKER_UNAVAILABLE);
  }
  terminate() {
    throw new Error(WORKER_UNAVAILABLE);
  }
  addEventListener() {
    throw new Error(WORKER_UNAVAILABLE);
  }
  removeEventListener() {
    throw new Error(WORKER_UNAVAILABLE);
  }
}
var Worker = loadWorkerConstructor();
var isWorkerRuntime = runtimeBridge !== undefined;
function postWorkerMessage(value) {
  if (!runtimeBridge) {
    throw new Error(WORKER_UNAVAILABLE);
  }
  runtimeBridge.postMessage(value);
}
function setWorkerMessageHandler(handler) {
  if (!runtimeBridge) {
    throw new Error(WORKER_UNAVAILABLE);
  }
  return runtimeBridge.setMessageHandler(handler);
}
function getBuiltinModule(id) {
  if (typeof process === "undefined") {
    return;
  }
  const loader = process.getBuiltinModule;
  if (typeof loader !== "function") {
    return;
  }
  try {
    return loader(id);
  } catch {
    return;
  }
}
function loadWorkerConstructor() {
  if (typeof globalWithWorker.Worker === "function") {
    return globalWithWorker.Worker;
  }
  if (nodeWorkerThreads) {
    return createNodeWorkerConstructor(nodeWorkerThreads);
  }
  return UnsupportedWorker;
}
function createNodeWorkerConstructor(node) {
  return class NodeWorkerShim {
    onmessage = null;
    onerror = null;
    errorListeners = new Set;
    messageListeners = new Set;
    worker;
    terminationPromise;
    constructor(specifier, options = {}) {
      const resolvedSpecifier = resolveWorkerImportSpecifier(specifier);
      this.worker = new node.Worker(createWorkerBootstrapSource(resolvedSpecifier), {
        eval: true,
        type: "module",
        name: options.name
      });
      this.worker.on("message", this.handleMessage);
      this.worker.on("error", this.handleError);
    }
    postMessage(value) {
      this.worker.postMessage(value);
    }
    terminate() {
      if (this.terminationPromise) {
        return this.terminationPromise;
      }
      this.worker.off("message", this.handleMessage);
      this.worker.off("error", this.handleError);
      const termination = this.worker.terminate().catch((error) => {
        this.terminationPromise = undefined;
        this.worker.on("message", this.handleMessage);
        this.worker.on("error", this.handleError);
        throw error;
      });
      this.terminationPromise = termination;
      return termination;
    }
    addEventListener(type, listener) {
      if (type === "message") {
        this.messageListeners.add(listener);
        return;
      }
      this.errorListeners.add(listener);
    }
    removeEventListener(type, listener) {
      if (type === "message") {
        this.messageListeners.delete(listener);
        return;
      }
      this.errorListeners.delete(listener);
    }
    handleMessage = (data) => {
      const event = { data };
      this.onmessage?.(event);
      for (const listener of this.messageListeners) {
        listener(event);
      }
    };
    handleError = (error) => {
      const event = {
        error,
        message: error.message
      };
      this.onerror?.(event);
      for (const listener of this.errorListeners) {
        listener(event);
      }
    };
  };
}
function createWorkerBootstrapSource(specifier) {
  return `
    import { parentPort } from "node:worker_threads"

    const pendingMessages = []
    let messageHandler = null

    globalThis.self ??= globalThis
    globalThis.postMessage ??= (value) => parentPort?.postMessage(value)
    globalThis.__opentuiWorkerMessageBridge = true
    Object.defineProperty(globalThis, "onmessage", {
      configurable: true,
      get: () => messageHandler,
      set: (handler) => {
        messageHandler = typeof handler === "function" ? handler : null
        if (!messageHandler) return

        const messages = pendingMessages.splice(0)
        for (const data of messages) {
          messageHandler({ data })
        }
      },
    })
    parentPort?.on("message", (data) => {
      if (messageHandler) {
        messageHandler({ data })
      } else {
        pendingMessages.push(data)
      }
    })

    await import(${JSON.stringify(specifier)})
  `;
}
function resolveWorkerImportSpecifier(specifier) {
  if (specifier instanceof URL) {
    return specifier.href;
  }
  if (isRuntimeSpecifier(specifier)) {
    return specifier;
  }
  const nodePath = getBuiltinModule("node:path");
  const nodeUrl = getBuiltinModule("node:url");
  if (!nodePath || !nodeUrl) {
    throw new Error(WORKER_UNAVAILABLE);
  }
  const absolutePath = nodePath.isAbsolute(specifier) ? specifier : nodePath.resolve(specifier);
  return nodeUrl.pathToFileURL(absolutePath).href;
}
function isRuntimeSpecifier(specifier) {
  return specifier.startsWith("file:") || specifier.startsWith("data:") || specifier.startsWith("node:") || specifier.startsWith("http:") || specifier.startsWith("https:");
}
function loadWorkerRuntime(node) {
  if (node?.parentPort && node.isMainThread === false) {
    if (globalWithWorker.__opentuiWorkerMessageBridge) {
      return createGlobalWorkerRuntimeBridge();
    }
    return {
      postMessage(value) {
        node.parentPort?.postMessage(value);
      },
      setMessageHandler(handler) {
        const listener = (data) => {
          handler({ data });
        };
        node.parentPort?.on("message", listener);
        return () => {
          node.parentPort?.off("message", listener);
        };
      }
    };
  }
  if (!isGlobalWorkerRuntime()) {
    return;
  }
  return createGlobalWorkerRuntimeBridge();
}
function createGlobalWorkerRuntimeBridge() {
  let currentRegistration;
  return {
    postMessage(value) {
      globalWithWorker.postMessage?.(value);
    },
    setMessageHandler(handler) {
      const previousHandler = getGlobalWorkerMessageHandler();
      if (currentRegistration && previousHandler !== currentRegistration.listener) {
        currentRegistration = undefined;
      }
      const listener = (event) => {
        const normalizedEvent = normalizeWorkerMessageEvent(event);
        handler(normalizedEvent);
      };
      const registration = {
        active: true,
        fallbackHandler: currentRegistration ? currentRegistration.fallbackHandler : previousHandler,
        listener,
        previous: currentRegistration
      };
      currentRegistration = registration;
      setGlobalWorkerMessageHandler(listener);
      return () => {
        registration.active = false;
        if (currentRegistration !== registration || getGlobalWorkerMessageHandler() !== listener) {
          return;
        }
        let previous = registration.previous;
        while (previous && !previous.active) {
          previous = previous.previous;
        }
        currentRegistration = previous;
        setGlobalWorkerMessageHandler(previous?.listener ?? registration.fallbackHandler);
      };
    }
  };
}
function isGlobalWorkerRuntime() {
  if (typeof globalWithWorker.postMessage !== "function") {
    return false;
  }
  return typeof document === "undefined" && typeof globalWithWorker.close === "function" && "onmessage" in globalThis;
}
function normalizeWorkerMessageEvent(event) {
  if (event && typeof event === "object" && "data" in event) {
    return event;
  }
  return { data: event };
}
function getGlobalWorkerMessageHandler() {
  return globalThis.onmessage ?? null;
}
function setGlobalWorkerMessageHandler(handler) {
  globalThis.onmessage = handler;
}

// src/lib/tree-sitter/parser.worker.ts
class ParserWorker {
  bufferParsers = new Map;
  filetypeParserOptions = new Map;
  filetypeAliases = new Map;
  filetypeParsers = new Map;
  filetypeParserPromises = new Map;
  reusableParsers = new Map;
  reusableParserPromises = new Map;
  initializePromise;
  performance;
  dataPath;
  tsDataPath;
  initialized = false;
  constructor() {
    this.performance = {
      averageParseTime: 0,
      parseTimes: [],
      averageQueryTime: 0,
      queryTimes: []
    };
  }
  async fetchQueries(sources, filetype) {
    if (!this.tsDataPath) {
      return "";
    }
    return DownloadUtils.fetchHighlightQueries(sources, this.tsDataPath, filetype);
  }
  async initialize({ dataPath }) {
    if (this.initializePromise) {
      return this.initializePromise;
    }
    this.initializePromise = (async () => {
      this.dataPath = dataPath;
      this.tsDataPath = path2.join(dataPath, "tree-sitter");
      await mkdir3(path2.join(this.tsDataPath, "languages"), { recursive: true });
      await mkdir3(path2.join(this.tsDataPath, "queries"), { recursive: true });
      let treeWasm = await resolveBundledFilePath(() => import("web-tree-sitter/tree-sitter.wasm", { with: { type: "wasm" } }), () => import.meta.resolve("web-tree-sitter/tree-sitter.wasm"), import.meta.url);
      if (isBunfsPath(treeWasm)) {
        treeWasm = normalizeBunfsPath(path2.parse(treeWasm).base);
      }
      await Parser.init({
        locateFile() {
          return treeWasm;
        }
      });
      this.initialized = true;
    })();
    return this.initializePromise;
  }
  addFiletypeParser(filetypeParser) {
    const previousAliases = this.filetypeParserOptions.get(filetypeParser.filetype)?.aliases ?? [];
    for (const alias of previousAliases) {
      if (this.filetypeAliases.get(alias) === filetypeParser.filetype) {
        this.filetypeAliases.delete(alias);
      }
    }
    const aliases = [...new Set((filetypeParser.aliases ?? []).filter((alias) => alias !== filetypeParser.filetype))];
    this.filetypeAliases.delete(filetypeParser.filetype);
    this.filetypeParserOptions.set(filetypeParser.filetype, {
      ...filetypeParser,
      aliases
    });
    for (const alias of aliases) {
      this.filetypeAliases.set(alias, filetypeParser.filetype);
    }
    this.invalidateParserCaches(filetypeParser.filetype);
  }
  resolveCanonicalFiletype(filetype) {
    if (this.filetypeParserOptions.has(filetype)) {
      return filetype;
    }
    return this.filetypeAliases.get(filetype) ?? filetype;
  }
  invalidateParserCaches(filetype) {
    this.filetypeParsers.delete(filetype);
    this.filetypeParserPromises.delete(filetype);
    const reusableParser = this.reusableParsers.get(filetype);
    if (reusableParser) {
      reusableParser.parser.delete();
      this.reusableParsers.delete(filetype);
    }
    this.reusableParserPromises.delete(filetype);
  }
  async createQueries(filetypeParser, language) {
    try {
      const highlightQueryContent = await this.fetchQueries(filetypeParser.queries.highlights, filetypeParser.filetype);
      if (!highlightQueryContent) {
        console.error("Failed to fetch highlight queries for:", filetypeParser.filetype);
        return;
      }
      const highlightsQuery = new Query(language, highlightQueryContent);
      const result = {
        highlights: highlightsQuery
      };
      if (filetypeParser.queries.injections && filetypeParser.queries.injections.length > 0) {
        const injectionQueryContent = await this.fetchQueries(filetypeParser.queries.injections, filetypeParser.filetype);
        if (injectionQueryContent) {
          result.injections = new Query(language, injectionQueryContent);
        }
      }
      return result;
    } catch (error) {
      console.error("Error creating queries for", filetypeParser.filetype, filetypeParser.queries);
      console.error(error);
      return;
    }
  }
  async loadLanguage(languageSource) {
    if (!this.initialized || !this.tsDataPath) {
      return;
    }
    const result = await DownloadUtils.downloadOrLoad(languageSource, this.tsDataPath, "languages", ".wasm", false);
    if (result.error) {
      console.error(`Error loading language ${languageSource}:`, result.error);
      return;
    }
    if (!result.filePath) {
      return;
    }
    const normalizedPath = result.filePath.replaceAll("\\", "/");
    try {
      const language = await Language.load(normalizedPath);
      return language;
    } catch (error) {
      console.error(`Error loading language from ${normalizedPath}:`, error);
      return;
    }
  }
  async resolveFiletypeParser(filetype) {
    const canonicalFiletype = this.resolveCanonicalFiletype(filetype);
    if (this.filetypeParsers.has(canonicalFiletype)) {
      return this.filetypeParsers.get(canonicalFiletype);
    }
    if (this.filetypeParserPromises.has(canonicalFiletype)) {
      return this.filetypeParserPromises.get(canonicalFiletype);
    }
    const loadingPromise = this.loadFiletypeParser(canonicalFiletype);
    this.filetypeParserPromises.set(canonicalFiletype, loadingPromise);
    try {
      const result = await loadingPromise;
      if (result) {
        this.filetypeParsers.set(canonicalFiletype, result);
      }
      return result;
    } finally {
      this.filetypeParserPromises.delete(canonicalFiletype);
    }
  }
  async loadFiletypeParser(filetype) {
    const filetypeParserOptions = this.filetypeParserOptions.get(filetype);
    if (!filetypeParserOptions) {
      return;
    }
    const language = await this.loadLanguage(filetypeParserOptions.wasm);
    if (!language) {
      return;
    }
    const queries = await this.createQueries(filetypeParserOptions, language);
    if (!queries) {
      console.error("Failed to create queries for:", filetype);
      return;
    }
    const filetypeParser = {
      ...filetypeParserOptions,
      queries,
      language
    };
    return filetypeParser;
  }
  async preloadParser(filetype) {
    return this.resolveFiletypeParser(filetype);
  }
  async getReusableParser(filetype) {
    const canonicalFiletype = this.resolveCanonicalFiletype(filetype);
    if (this.reusableParsers.has(canonicalFiletype)) {
      return this.reusableParsers.get(canonicalFiletype);
    }
    if (this.reusableParserPromises.has(canonicalFiletype)) {
      return this.reusableParserPromises.get(canonicalFiletype);
    }
    const creationPromise = this.createReusableParser(canonicalFiletype);
    this.reusableParserPromises.set(canonicalFiletype, creationPromise);
    try {
      const result = await creationPromise;
      if (result) {
        this.reusableParsers.set(canonicalFiletype, result);
      }
      return result;
    } finally {
      this.reusableParserPromises.delete(canonicalFiletype);
    }
  }
  async createReusableParser(filetype) {
    const filetypeParser = await this.resolveFiletypeParser(filetype);
    if (!filetypeParser) {
      return;
    }
    const parser = new Parser;
    parser.setLanguage(filetypeParser.language);
    const reusableState = {
      parser,
      filetypeParser,
      queries: filetypeParser.queries
    };
    return reusableState;
  }
  async handleInitializeParser(bufferId, version, content, filetype, messageId) {
    const filetypeParser = await this.resolveFiletypeParser(filetype);
    if (!filetypeParser) {
      postWorkerMessage({
        type: "PARSER_INIT_RESPONSE",
        bufferId,
        messageId,
        hasParser: false,
        warning: `No parser available for filetype ${filetype}`
      });
      return;
    }
    const parser = new Parser;
    parser.setLanguage(filetypeParser.language);
    const tree = parser.parse(content);
    if (!tree) {
      postWorkerMessage({
        type: "PARSER_INIT_RESPONSE",
        bufferId,
        messageId,
        hasParser: false,
        error: "Failed to parse buffer"
      });
      return;
    }
    const parserState = {
      parser,
      tree,
      queries: filetypeParser.queries,
      filetype,
      content,
      injectionMapping: filetypeParser.injectionMapping
    };
    this.bufferParsers.set(bufferId, parserState);
    postWorkerMessage({
      type: "PARSER_INIT_RESPONSE",
      bufferId,
      messageId,
      hasParser: true
    });
    const highlights = await this.initialQuery(parserState);
    postWorkerMessage({
      type: "HIGHLIGHT_RESPONSE",
      bufferId,
      version,
      ...highlights
    });
  }
  async initialQuery(parserState) {
    const query = parserState.queries.highlights;
    const matches = query.captures(parserState.tree.rootNode);
    let injectionRanges = new Map;
    if (parserState.queries.injections) {
      const injectionResult = await this.processInjections(parserState);
      matches.push(...injectionResult.captures);
      injectionRanges = injectionResult.injectionRanges;
    }
    return this.getHighlights(parserState, matches, injectionRanges);
  }
  getNodeText(node, content) {
    return content.substring(node.startIndex, node.endIndex);
  }
  async processInjections(parserState) {
    const injectionMatches = [];
    const injectionRanges = new Map;
    if (!parserState.queries.injections) {
      return { captures: injectionMatches, injectionRanges };
    }
    const content = parserState.content;
    const injectionCaptures = parserState.queries.injections.captures(parserState.tree.rootNode);
    const languageGroups = new Map;
    const injectionMapping = parserState.injectionMapping;
    for (const capture of injectionCaptures) {
      const captureName = capture.name;
      if (captureName === "injection.content" || captureName.includes("injection")) {
        const nodeType = capture.node.type;
        let targetLanguage;
        if (injectionMapping?.nodeTypes && injectionMapping.nodeTypes[nodeType]) {
          targetLanguage = injectionMapping.nodeTypes[nodeType];
        } else if (nodeType === "code_fence_content") {
          const parent = capture.node.parent;
          if (parent) {
            const infoString = parent.children.find((child) => child.type === "info_string");
            if (infoString) {
              const languageNode = infoString.children.find((child) => child.type === "language");
              if (languageNode) {
                const languageName = this.getNodeText(languageNode, content);
                if (injectionMapping?.infoStringMap && injectionMapping.infoStringMap[languageName]) {
                  targetLanguage = injectionMapping.infoStringMap[languageName];
                } else {
                  targetLanguage = languageName;
                }
              }
            }
          }
        }
        if (targetLanguage) {
          if (!languageGroups.has(targetLanguage)) {
            languageGroups.set(targetLanguage, []);
          }
          languageGroups.get(targetLanguage).push({ node: capture.node, name: capture.name });
        }
      }
    }
    for (const [language, captures] of languageGroups.entries()) {
      const injectedParser = await this.getReusableParser(language);
      if (!injectedParser) {
        console.warn(`No parser found for injection language: ${language}`);
        continue;
      }
      if (!injectionRanges.has(language)) {
        injectionRanges.set(language, []);
      }
      const parser = injectedParser.parser;
      for (const { node: injectionNode } of captures) {
        try {
          injectionRanges.get(language).push({
            start: injectionNode.startIndex,
            end: injectionNode.endIndex
          });
          const injectionContent = this.getNodeText(injectionNode, content);
          const tree = parser.parse(injectionContent);
          if (tree) {
            const matches = injectedParser.queries.highlights.captures(tree.rootNode);
            for (const match of matches) {
              const offsetCapture = {
                name: match.name,
                patternIndex: match.patternIndex,
                _injectedQuery: injectedParser.queries.highlights,
                node: {
                  ...match.node,
                  startPosition: {
                    row: match.node.startPosition.row + injectionNode.startPosition.row,
                    column: match.node.startPosition.row === 0 ? match.node.startPosition.column + injectionNode.startPosition.column : match.node.startPosition.column
                  },
                  endPosition: {
                    row: match.node.endPosition.row + injectionNode.startPosition.row,
                    column: match.node.endPosition.row === 0 ? match.node.endPosition.column + injectionNode.startPosition.column : match.node.endPosition.column
                  },
                  startIndex: match.node.startIndex + injectionNode.startIndex,
                  endIndex: match.node.endIndex + injectionNode.startIndex
                }
              };
              injectionMatches.push(offsetCapture);
            }
            tree.delete();
          }
        } catch (error) {
          console.error(`Error processing injection for language ${language}:`, error);
        }
      }
    }
    return { captures: injectionMatches, injectionRanges };
  }
  editToRange(edit) {
    return {
      startPosition: {
        column: edit.startPosition.column,
        row: edit.startPosition.row
      },
      endPosition: {
        column: edit.newEndPosition.column,
        row: edit.newEndPosition.row
      },
      startIndex: edit.startIndex,
      endIndex: edit.newEndIndex
    };
  }
  async handleEdits(bufferId, content, edits) {
    const parserState = this.bufferParsers.get(bufferId);
    if (!parserState) {
      return { warning: "No parser state found for buffer" };
    }
    parserState.content = content;
    for (const edit of edits) {
      parserState.tree.edit(edit);
    }
    const startParse = performance.now();
    const newTree = parserState.parser.parse(content, parserState.tree);
    const endParse = performance.now();
    const parseTime = endParse - startParse;
    this.performance.parseTimes.push(parseTime);
    if (this.performance.parseTimes.length > 10) {
      this.performance.parseTimes.shift();
    }
    this.performance.averageParseTime = this.performance.parseTimes.reduce((acc, time) => acc + time, 0) / this.performance.parseTimes.length;
    if (!newTree) {
      return { error: "Failed to parse buffer" };
    }
    const changedRanges = parserState.tree.getChangedRanges(newTree);
    parserState.tree = newTree;
    const startQuery = performance.now();
    const matches = [];
    if (changedRanges.length === 0) {
      edits.forEach((edit) => {
        const range = this.editToRange(edit);
        changedRanges.push(range);
      });
    }
    for (const range of changedRanges) {
      let node = parserState.tree.rootNode.descendantForPosition(range.startPosition, range.endPosition);
      if (!node) {
        continue;
      }
      if (node.equals(parserState.tree.rootNode)) {
        const rangeCaptures = parserState.queries.highlights.captures(node, {
          startIndex: range.startIndex - 100,
          endIndex: range.endIndex + 1000
        });
        matches.push(...rangeCaptures);
        continue;
      }
      while (node && !this.nodeContainsRange(node, range)) {
        node = node.parent;
      }
      if (!node) {
        node = parserState.tree.rootNode;
      }
      const nodeCaptures = parserState.queries.highlights.captures(node);
      matches.push(...nodeCaptures);
    }
    let injectionRanges = new Map;
    if (parserState.queries.injections) {
      const injectionResult = await this.processInjections(parserState);
      matches.push(...injectionResult.captures);
      injectionRanges = injectionResult.injectionRanges;
    }
    const endQuery = performance.now();
    const queryTime = endQuery - startQuery;
    this.performance.queryTimes.push(queryTime);
    if (this.performance.queryTimes.length > 10) {
      this.performance.queryTimes.shift();
    }
    this.performance.averageQueryTime = this.performance.queryTimes.reduce((acc, time) => acc + time, 0) / this.performance.queryTimes.length;
    return this.getHighlights(parserState, matches, injectionRanges);
  }
  nodeContainsRange(node, range) {
    return node.startPosition.row <= range.startPosition.row && node.endPosition.row >= range.endPosition.row && (node.startPosition.row < range.startPosition.row || node.startPosition.column <= range.startPosition.column) && (node.endPosition.row > range.endPosition.row || node.endPosition.column >= range.endPosition.column);
  }
  getHighlights(parserState, matches, injectionRanges) {
    const lineHighlights = new Map;
    const droppedHighlights = new Map;
    for (const match of matches) {
      const node = match.node;
      const startLine = node.startPosition.row;
      const endLine = node.endPosition.row;
      const highlight = {
        startCol: node.startPosition.column,
        endCol: node.endPosition.column,
        group: match.name
      };
      if (!lineHighlights.has(startLine)) {
        lineHighlights.set(startLine, new Map);
        droppedHighlights.set(startLine, new Map);
      }
      if (lineHighlights.get(startLine)?.has(node.id)) {
        droppedHighlights.get(startLine)?.set(node.id, lineHighlights.get(startLine)?.get(node.id));
      }
      lineHighlights.get(startLine)?.set(node.id, highlight);
      if (startLine !== endLine) {
        for (let line = startLine + 1;line <= endLine; line++) {
          if (!lineHighlights.has(line)) {
            lineHighlights.set(line, new Map);
          }
          const hl = {
            startCol: 0,
            endCol: node.endPosition.column,
            group: match.name
          };
          lineHighlights.get(line)?.set(node.id, hl);
        }
      }
    }
    return {
      highlights: Array.from(lineHighlights.entries()).map(([line, lineHighlights2]) => ({
        line,
        highlights: Array.from(lineHighlights2.values()),
        droppedHighlights: droppedHighlights.get(line) ? Array.from(droppedHighlights.get(line).values()) : []
      }))
    };
  }
  getSimpleHighlights(matches, injectionRanges) {
    const highlights = [];
    const flatInjectionRanges = [];
    for (const [lang, ranges] of injectionRanges.entries()) {
      for (const range of ranges) {
        flatInjectionRanges.push({ ...range, lang });
      }
    }
    for (const match of matches) {
      const node = match.node;
      let isInjection = false;
      let injectionLang;
      let containsInjection = false;
      for (const injRange of flatInjectionRanges) {
        if (node.startIndex >= injRange.start && node.endIndex <= injRange.end) {
          isInjection = true;
          injectionLang = injRange.lang;
          break;
        } else if (node.startIndex <= injRange.start && node.endIndex >= injRange.end) {
          containsInjection = true;
          break;
        }
      }
      const matchQuery = match._injectedQuery;
      const patternProperties = matchQuery?.setProperties?.[match.patternIndex];
      const concealValue = patternProperties?.conceal ?? match.setProperties?.conceal;
      const concealLines = patternProperties?.conceal_lines ?? match.setProperties?.conceal_lines;
      const meta = {};
      if (isInjection && injectionLang) {
        meta.isInjection = true;
        meta.injectionLang = injectionLang;
      }
      if (containsInjection) {
        meta.containsInjection = true;
      }
      if (concealValue !== undefined) {
        meta.conceal = concealValue;
      }
      if (concealLines !== undefined) {
        meta.concealLines = concealLines;
      }
      if (Object.keys(meta).length > 0) {
        highlights.push([node.startIndex, node.endIndex, match.name, meta]);
      } else {
        highlights.push([node.startIndex, node.endIndex, match.name]);
      }
    }
    highlights.sort((a, b) => a[0] - b[0]);
    return highlights;
  }
  async handleResetBuffer(bufferId, version, content) {
    const parserState = this.bufferParsers.get(bufferId);
    if (!parserState) {
      return { warning: "No parser state found for buffer" };
    }
    parserState.content = content;
    const newTree = parserState.parser.parse(content);
    if (!newTree) {
      return { error: "Failed to parse buffer during reset" };
    }
    parserState.tree = newTree;
    const matches = parserState.queries.highlights.captures(parserState.tree.rootNode);
    let injectionRanges = new Map;
    if (parserState.queries.injections) {
      const injectionResult = await this.processInjections(parserState);
      matches.push(...injectionResult.captures);
      injectionRanges = injectionResult.injectionRanges;
    }
    return this.getHighlights(parserState, matches, injectionRanges);
  }
  disposeBuffer(bufferId) {
    const parserState = this.bufferParsers.get(bufferId);
    if (!parserState) {
      return;
    }
    parserState.tree.delete();
    parserState.parser.delete();
    this.bufferParsers.delete(bufferId);
  }
  async handleOneShotHighlight(content, filetype, messageId) {
    const reusableState = await this.getReusableParser(filetype);
    if (!reusableState) {
      postWorkerMessage({
        type: "ONESHOT_HIGHLIGHT_RESPONSE",
        messageId,
        hasParser: false,
        warning: `No parser available for filetype ${filetype}`
      });
      return;
    }
    const parseContent = filetype === "markdown" && content.endsWith("```") ? content + `
` : content;
    const tree = reusableState.parser.parse(parseContent);
    if (!tree) {
      postWorkerMessage({
        type: "ONESHOT_HIGHLIGHT_RESPONSE",
        messageId,
        hasParser: false,
        error: "Failed to parse content"
      });
      return;
    }
    try {
      const matches = reusableState.filetypeParser.queries.highlights.captures(tree.rootNode);
      let injectionRanges = new Map;
      if (reusableState.filetypeParser.queries.injections) {
        const parserState = {
          parser: reusableState.parser,
          tree,
          queries: reusableState.filetypeParser.queries,
          filetype,
          content,
          injectionMapping: reusableState.filetypeParser.injectionMapping
        };
        const injectionResult = await this.processInjections(parserState);
        matches.push(...injectionResult.captures);
        injectionRanges = injectionResult.injectionRanges;
      }
      const highlights = this.getSimpleHighlights(matches, injectionRanges);
      postWorkerMessage({
        type: "ONESHOT_HIGHLIGHT_RESPONSE",
        messageId,
        hasParser: true,
        highlights
      });
    } finally {
      tree.delete();
    }
  }
  async updateDataPath(dataPath) {
    this.dataPath = dataPath;
    this.tsDataPath = path2.join(dataPath, "tree-sitter");
    try {
      await mkdir3(path2.join(this.tsDataPath, "languages"), { recursive: true });
      await mkdir3(path2.join(this.tsDataPath, "queries"), { recursive: true });
    } catch (error) {
      throw new Error(`Failed to update data path: ${error}`);
    }
  }
  async clearCache() {
    if (!this.dataPath || !this.tsDataPath) {
      throw new Error("No data path configured");
    }
    const { rm } = await import("fs/promises");
    try {
      const treeSitterPath = path2.join(this.dataPath, "tree-sitter");
      await rm(treeSitterPath, { recursive: true, force: true });
      await mkdir3(path2.join(treeSitterPath, "languages"), { recursive: true });
      await mkdir3(path2.join(treeSitterPath, "queries"), { recursive: true });
      this.filetypeParsers.clear();
      this.filetypeParserPromises.clear();
      this.reusableParsers.clear();
      this.reusableParserPromises.clear();
    } catch (error) {
      throw new Error(`Failed to clear cache: ${error}`);
    }
  }
}
function logMessage(type, ...args) {
  postWorkerMessage({
    type: "WORKER_LOG",
    logType: type,
    data: args
  });
}
function postWorkerError(bufferId, error) {
  postWorkerMessage({
    type: "ERROR",
    bufferId,
    error: error instanceof Error ? error.stack || error.message : String(error)
  });
}
if (isWorkerRuntime) {
  const worker = new ParserWorker;
  console.log = (...args) => logMessage("log", ...args);
  console.error = (...args) => logMessage("error", ...args);
  console.warn = (...args) => logMessage("warn", ...args);
  setWorkerMessageHandler(async (event) => {
    const message = event.data;
    const messageType = String(event.data.type ?? "unknown");
    try {
      switch (message.type) {
        case "INIT":
          try {
            await worker.initialize({ dataPath: message.dataPath });
            postWorkerMessage({ type: "INIT_RESPONSE" });
          } catch (error) {
            postWorkerMessage({
              type: "INIT_RESPONSE",
              error: error instanceof Error ? error.stack || error.message : String(error)
            });
          }
          break;
        case "ADD_FILETYPE_PARSER":
          worker.addFiletypeParser(message.filetypeParser);
          break;
        case "PRELOAD_PARSER": {
          const maybeParser = await worker.preloadParser(message.filetype);
          postWorkerMessage({
            type: "PRELOAD_PARSER_RESPONSE",
            messageId: message.messageId,
            hasParser: !!maybeParser
          });
          break;
        }
        case "INITIALIZE_PARSER":
          await worker.handleInitializeParser(message.bufferId, message.version, message.content, message.filetype, message.messageId);
          break;
        case "HANDLE_EDITS": {
          const response = await worker.handleEdits(message.bufferId, message.content, message.edits);
          if (response.highlights && response.highlights.length > 0) {
            postWorkerMessage({
              type: "HIGHLIGHT_RESPONSE",
              bufferId: message.bufferId,
              version: message.version,
              highlights: response.highlights
            });
          } else if (response.warning) {
            postWorkerMessage({
              type: "WARNING",
              bufferId: message.bufferId,
              warning: response.warning
            });
          } else if (response.error) {
            postWorkerMessage({
              type: "ERROR",
              bufferId: message.bufferId,
              error: response.error
            });
          }
          break;
        }
        case "GET_PERFORMANCE":
          postWorkerMessage({
            type: "PERFORMANCE_RESPONSE",
            performance: worker.performance,
            messageId: message.messageId
          });
          break;
        case "RESET_BUFFER": {
          const resetResponse = await worker.handleResetBuffer(message.bufferId, message.version, message.content);
          if (resetResponse.highlights && resetResponse.highlights.length > 0) {
            postWorkerMessage({
              type: "HIGHLIGHT_RESPONSE",
              bufferId: message.bufferId,
              version: message.version,
              highlights: resetResponse.highlights
            });
          } else if (resetResponse.warning) {
            postWorkerMessage({
              type: "WARNING",
              bufferId: message.bufferId,
              warning: resetResponse.warning
            });
          } else if (resetResponse.error) {
            postWorkerMessage({
              type: "ERROR",
              bufferId: message.bufferId,
              error: resetResponse.error
            });
          }
          break;
        }
        case "DISPOSE_BUFFER":
          worker.disposeBuffer(message.bufferId);
          postWorkerMessage({
            type: "BUFFER_DISPOSED",
            bufferId: message.bufferId
          });
          break;
        case "ONESHOT_HIGHLIGHT":
          await worker.handleOneShotHighlight(message.content, message.filetype, message.messageId);
          break;
        case "UPDATE_DATA_PATH":
          try {
            await worker.updateDataPath(message.dataPath);
            postWorkerMessage({
              type: "UPDATE_DATA_PATH_RESPONSE",
              messageId: message.messageId
            });
          } catch (error) {
            postWorkerMessage({
              type: "UPDATE_DATA_PATH_RESPONSE",
              messageId: message.messageId,
              error: error instanceof Error ? error.message : String(error)
            });
          }
          break;
        case "CLEAR_CACHE":
          try {
            await worker.clearCache();
            postWorkerMessage({
              type: "CLEAR_CACHE_RESPONSE",
              messageId: message.messageId
            });
          } catch (error) {
            postWorkerMessage({
              type: "CLEAR_CACHE_RESPONSE",
              messageId: message.messageId,
              error: error instanceof Error ? error.message : String(error)
            });
          }
          break;
        default:
          postWorkerMessage({
            type: "ERROR",
            error: `Unknown message type: ${messageType}`
          });
      }
    } catch (error) {
      if ("bufferId" in message) {
        postWorkerError(message.bufferId, error);
      } else {
        postWorkerError(undefined, error);
      }
    }
  });
}

//# debugId=0D88AD8518CAE38D64756E2164756E21
//# sourceMappingURL=parser.worker.js.map
