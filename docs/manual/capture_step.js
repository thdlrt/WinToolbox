async page => {
  await page.locator('section').filter({has:page.getByRole('heading',{name:'处理步骤',exact:true})}).screenshot({path:'output/playwright/manual/03-processing.png',animations:'disabled'});
  await page.locator('section').filter({has:page.getByRole('heading',{name:'识别与输出',exact:true})}).screenshot({path:'output/playwright/manual/03-output.png',animations:'disabled'});
  return 'Captured workflow controls';
}
