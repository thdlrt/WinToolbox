import { strict as assert } from 'node:assert';
import { expenseMoney, expenseSettlement } from './expensesState.ts';

assert.deepEqual(expenseSettlement('25.35'), { kind: 'due', label: '还需转入', amount: '25.35' });
assert.deepEqual(expenseSettlement('-25.35'), { kind: 'extra', label: '多转金额', amount: '25.35' });
assert.deepEqual(expenseSettlement('0.00'), { kind: 'settled', label: '已结清', amount: '0.00' });
assert.deepEqual(expenseSettlement('-0.00'), { kind: 'settled', label: '已结清', amount: '0.00' });
assert.deepEqual(expenseSettlement('+0.00'), { kind: 'settled', label: '已结清', amount: '0.00' });
assert.equal(expenseSettlement('0.01').amount, '0.01', 'one-cent shortfall remains visible');
assert.equal(expenseSettlement('-0.01').amount, '0.01', 'one-cent overpayment uses an unsigned amount');
assert.equal(expenseSettlement('-0.01').label, '多转金额');
assert.equal(expenseSettlement('1000000000.01').amount, '1,000,000,000.01');
assert.equal(expenseSettlement('-9007199254740993.01').amount, '9,007,199,254,740,993.01', 'formatting does not lose precision beyond Number safe integers');
assert.equal(expenseSettlement(' 12.50 ').amount, '12.50');
assert.equal(expenseMoney('10.01'), '10.01', 'backend-rounded share_due is displayed without recalculating');
assert.equal(expenseMoney('123456789.99'), '123,456,789.99');
console.log('Expense settlement: 13 assertions passed; no floating-point money arithmetic.');
