export const pages = {overview:'概览', policy:'策略', log:'调用记录', audit:'审计日志', desktop:'登录桌面'};
export const fields = {
  concurrency:'同时执行的生成数', min_interval_seconds:'两次提交最小间隔（秒）', daily_submit_limit:'每日提交上限（次）',
  per_job_credit_limit:'单任务积分上限', daily_credit_limit:'每日积分上限', on_unusual_activity:'遇到 403 UNUSUAL_ACTIVITY',
  cooldown_minutes:'自动冷却时长（分钟）', on_submission_unknown:'提交结果不明时'
};
export const choices = {on_unusual_activity:{pause:'暂停渠道',continue:'只记录'},on_submission_unknown:{hold:'等待对账',pause:'同时暂停渠道'}};
export const reasons = {manual:'管理员手动暂停',unusual_activity:'Flow 返回异常活动',authorization_required:'需要重新授权',
  credit_limit:'达到积分上限',submission_unknown:'提交结果不明',gpu_lease_lost:'GPU 租约丢失'};
export async function request(sdk, method, path, body) {
  if (!sdk) throw Error('请从 Studio 应用中心打开');
  const args = {method, path};
  if (body !== undefined) args.body = body;
  const response = await sdk.app.request(args);
  if (response.status < 200 || response.status >= 300) throw Error(response.data?.code || '请求失败');
  return response.data;
}
