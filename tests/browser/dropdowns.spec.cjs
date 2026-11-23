const { expect, test } = require("@playwright/test");
const path = require("node:path");

async function mountPicker(page, options, { atEdge = false, forceFallback = false } = {}) {
  const choices = options.map((label, index) => `
    <li role="none">
      <button type="button" role="menuitemradio" tabindex="-1" aria-checked="${index === 0}"
        data-opt-value="${index}" data-opt-label="${label}"
        onclick="setDropdownValue('choice', '#choice-display', this)">${label}</button>
    </li>`).join("");
  await page.setContent(`
    <form id="filters"${atEdge ? ' style="position:fixed;right:2px;bottom:2px"' : ""}>
      <button type="button" id="choice-button" popovertarget="choice-picker" aria-haspopup="menu"
        aria-expanded="false" aria-controls="choice-picker" style="anchor-name:--choice;width:220px">
        <span id="choice-display">${options[0]}</span>
      </button>
      <div id="choice-picker" popover class="gys-picker" role="menu"
        aria-labelledby="choice-button" style="position-anchor:--choice">
        <div class="gys-picker-search" hidden>
          <input type="search" aria-label="Search options">
          <p class="gys-picker-status" role="status"></p>
        </div>
        <ul id="choice-menu" class="gys-picker-options" role="group" aria-label="Options">${choices}</ul>
      </div>
      <input id="choice" type="hidden" value="0">
      <button type="button">Next field</button>
    </form>
  `);
  await page.addStyleTag({ path: path.resolve(__dirname, "../../app/static/css/app.css") });
  if (forceFallback) {
    await page.evaluate(() => {
      const supports = CSS.supports.bind(CSS);
      CSS.supports = (prop, value) => prop !== "position-area" && supports(prop, value);
    });
  }
  await page.addScriptTag({ path: path.resolve(__dirname, "../../app/static/js/dropdowns.js") });
  return { trigger: page.locator("#choice-button"), picker: page.locator("#choice-picker") };
}

test("short pickers keep selection separate from focus and support typeahead", async ({ page }) => {
  const { trigger, picker } = await mountPicker(page, ["Alpha", "Beta", "Charlie"]);
  const alpha = picker.getByRole("menuitemradio", { name: "Alpha" });
  const beta = picker.getByRole("menuitemradio", { name: "Beta" });
  const charlie = picker.getByRole("menuitemradio", { name: "Charlie" });

  await trigger.press("Enter");
  await expect(alpha).toBeFocused();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await expect(picker.getByRole("searchbox")).toHaveCount(0);
  await expect(alpha).toBeChecked();
  await expect.poll(() => alpha.evaluate((el) => getComputedStyle(el, "::after").borderBlockEndWidth)).toBe("2px");

  await page.keyboard.press("c");
  await expect(charlie).toBeFocused();
  await expect(alpha).toBeChecked();
  await expect(charlie).not.toBeChecked();
  await page.keyboard.press("Home");
  await page.keyboard.press("ArrowDown");
  await expect(beta).toBeFocused();
  await page.keyboard.press("Space");
  await expect(picker).not.toBeVisible();
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("#choice")).toHaveValue("1");
  await expect(trigger).toHaveText("Beta");

  await trigger.press("ArrowDown");
  await expect(beta).toBeFocused();
  await expect(beta).toBeChecked();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await trigger.press("Space");
  await expect(beta).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(picker).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Next field" })).toBeFocused();
});

test("long pickers filter without changing the form until an option is selected", async ({ page }) => {
  const names = Array.from({ length: 9 }, (_, index) => `Agent ${index + 1}`).concat("Casey Example");
  const { trigger, picker } = await mountPicker(page, names);
  await page.evaluate(() => {
    for (const type of ["input", "change", "search"]) {
      document.querySelector("form").addEventListener(type, (event) => {
        document.body.dataset.changed = event.target.id || "search";
      });
    }
  });
  await trigger.press("Enter");
  await expect(picker).toHaveRole("dialog");
  const search = picker.getByRole("searchbox");
  await expect(search).toBeFocused();
  const fontSize = await trigger.evaluate((el) => getComputedStyle(el).fontSize);
  await expect(search).toHaveCSS("font-size", fontSize);
  await expect(picker.getByRole("menuitemradio").first()).toHaveCSS("font-size", fontSize);
  await search.fill("does not exist");
  await expect(picker.getByRole("status")).toHaveText("No matching options.");
  await search.press("Enter");
  await expect(picker).toBeVisible();

  await search.fill("  EXAMPLE ");
  await expect(picker.getByRole("menuitemradio")).toHaveCount(1);
  await search.press("Tab");
  await expect(picker.getByRole("menuitemradio", { name: "Casey Example" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(search).toBeFocused();
  await search.press("ArrowDown");
  await expect(picker.getByRole("menuitemradio", { name: "Casey Example" })).toBeFocused();
  await expect(page.locator("body")).not.toHaveAttribute("data-changed");
  await page.keyboard.press("Enter");
  await expect(page.locator("#choice")).toHaveValue("9");
  await expect(page.locator("body")).toHaveAttribute("data-changed", "choice");

  await trigger.click();
  await expect(search).toHaveValue("");
  await expect(picker.getByRole("menuitemradio", { name: "Casey Example" })).toBeChecked();
  await expect(picker.getByRole("menuitemradio")).toHaveCount(10);
});

for (const forceFallback of [false, true]) {
  test(`picker fits the viewport and wraps long labels${forceFallback ? " without anchor positioning" : ""}`, async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 400 });
    const long = "AVeryLongAgentNameWithoutSpaces".repeat(5);
    const { trigger, picker } = await mountPicker(page, ["Alpha", long], { atEdge: true, forceFallback });
    await trigger.click();
    const anchor = await trigger.boundingBox();
    await expect.poll(async () => {
      const box = await picker.boundingBox();
      return box && box.width >= anchor.width && box.width <= anchor.width + 24.5
        && box.x >= 8 && box.x + box.width <= 312
        && box.y >= 8 && box.y + box.height <= anchor.y;
    }).toBe(true);
    const option = picker.getByRole("menuitemradio", { name: long });
    const fits = await option.evaluate((el) => ({
      wraps: el.offsetHeight > 40,
      overflow: el.scrollWidth > el.clientWidth,
    }));
    expect(fits).toEqual({ wraps: true, overflow: false });
  });
}

test("a picker closes when its trigger scrolls out of a dialog", async ({ page }) => {
  const { trigger, picker } = await mountPicker(page, ["Alpha", "Beta", "Charlie"]);
  await page.evaluate(() => {
    const dialog = document.createElement("dialog");
    const scrollport = document.createElement("div");
    scrollport.style.cssText = "height: 120px; overflow: auto";
    scrollport.append(document.querySelector("#filters"));
    const spacer = document.createElement("div");
    spacer.style.height = "300px";
    scrollport.append(spacer);
    dialog.append(scrollport);
    document.body.append(dialog);
    dialog.showModal();
  });
  await trigger.press("Enter");
  await expect(picker.getByRole("menuitemradio", { name: "Alpha" })).toBeFocused();
  await page.locator("dialog > div").evaluate((scrollport) => { scrollport.scrollTop = 200; });
  await expect(picker).not.toBeVisible();
  await expect(page.locator("dialog")).toBeVisible();
  await expect.poll(() => page.locator("dialog > div").evaluate((el) => el.scrollTop)).toBe(200);
});
