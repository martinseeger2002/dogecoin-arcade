// node --import ./extension/test/register.mjs ...: the extension's absolute "/vendor/" imports, for Node.
import {register} from "node:module";
register("./hooks.mjs", import.meta.url);
