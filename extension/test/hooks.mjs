// For the tests under Node: coins.js imports "/vendor/..." from the extension's root, as a browser resolves it there.
export async function resolve(specifier, context, next) {
  if (specifier.startsWith("/vendor/")) return next(new URL(`../${specifier.slice(1)}`, import.meta.url).href, context);
  return next(specifier, context);
}
