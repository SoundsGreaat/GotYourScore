const { expect, test } = require("@playwright/test");
const path = require("node:path");

test("shared dropdown supports menu keyboard navigation and accessible state", async ({ page }) => {
  await page.setContent(`
    <button id="choice-button" popovertarget="choice-menu" aria-haspopup="menu"
      aria-expanded="false" aria-controls="choice-menu">Choose</button>
    <ul id="choice-menu" popover role="menu" aria-labelledby="choice-button"
      class="dropdown menu">
      <li><button type="button" role="menuitem" tabindex="-1" data-opt-value="a"
        data-opt-label="Alpha" class="active menu-active" onclick="setDropdownValue('choice', '#choice-display', this)">Alpha</button></li>
      <li><button type="button" role="menuitem" tabindex="-1" data-opt-value="b"
        data-opt-label="Beta" onclick="setDropdownValue('choice', '#choice-display', this)">Beta</button></li>
      <li><button type="button" role="menuitem" tabindex="-1" data-opt-value="c"
        data-opt-label="Charlie" onclick="setDropdownValue('choice', '#choice-display', this)">Charlie</button></li>
    </ul>
    <span id="choice-display">Alpha</span>
    <input id="choice" type="hidden" value="a">
    <button type="button">Next field</button>
  `);
  await page.addScriptTag({ path: path.resolve(__dirname, "../../app/static/js/dropdowns.js") });

  const trigger = page.locator("#choice-button");
  const menu = page.locator("#choice-menu");
  const alpha = menu.getByRole("menuitem", { name: "Alpha" });
  const beta = menu.getByRole("menuitem", { name: "Beta" });
  const charlie = menu.getByRole("menuitem", { name: "Charlie" });

  await trigger.focus();
  await page.keyboard.press("Enter");
  await expect(menu).toBeVisible();
  await expect(alpha).toBeFocused();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");

  await page.keyboard.press("End");
  await expect(charlie).toBeFocused();
  await page.keyboard.press("Home");
  await expect(alpha).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(beta).toBeFocused();
  await page.keyboard.press("Space");
  await expect(menu).not.toBeVisible();
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("#choice")).toHaveValue("b");
  await expect(page.locator("#choice-display")).toHaveText("Beta");

  await trigger.focus();
  await page.keyboard.press("ArrowDown");
  await expect(beta).toBeFocused();
  await page.keyboard.press("Home");
  await expect(alpha).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(menu).not.toBeVisible();
  await expect(trigger).toBeFocused();
  await trigger.press("Space");
  await expect(beta).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(menu).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Next field" })).toBeFocused();
});

test("shared dropdown typeahead moves focus to the matching option", async ({ page }) => {
  await page.setContent(`
    <button id="choice-button" popovertarget="choice-menu" aria-haspopup="menu">Choose</button>
    <ul id="choice-menu" popover class="dropdown menu">
      <li><button type="button" role="menuitem" tabindex="-1" data-opt-label="Alpha">Alpha</button></li>
      <li><button type="button" role="menuitem" tabindex="-1" data-opt-label="Beta">Beta</button></li>
      <li><button type="button" role="menuitem" tabindex="-1" data-opt-label="Charlie">Charlie</button></li>
    </ul>
  `);
  await page.addScriptTag({ path: path.resolve(__dirname, "../../app/static/js/dropdowns.js") });
  const trigger = page.locator("#choice-button");
  await trigger.focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("c");
  await expect(page.getByRole("menuitem", { name: "Charlie" })).toBeFocused();
});
