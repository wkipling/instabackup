// Dump assembly for a function by address/symbol.
// Usage: -postScript DumpAsm.java <outDir> <addressListFile>
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.PrintWriter;

public class DumpAsm extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]);
        outDir.mkdirs();
        try (BufferedReader br = new BufferedReader(new FileReader(args[1]))) {
            String line;
            while ((line = br.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) continue;
                String[] p = line.split("\\s+", 2);
                Address a = toAddr(p[0]);
                String hint = p.length > 1 ? p[1] : p[0];
                Function f = getFunctionAt(a);
                if (f == null) f = getFunctionContaining(a);
                if (f == null) { println("NOFUNC " + a); continue; }
                File out = new File(outDir, String.format("%08x_%s.asm", f.getEntryPoint().getOffset(),
                    hint.replaceAll("[^A-Za-z0-9_.-]", "_")));
                try (PrintWriter w = new PrintWriter(out)) {
                    w.println("// " + hint + " @ " + f.getEntryPoint());
                    InstructionIterator it = currentProgram.getListing().getInstructions(f.getBody(), true);
                    while (it.hasNext()) {
                        Instruction ins = it.next();
                        w.println(ins.getAddress() + "  " + ins.toString());
                    }
                }
                println("WROTE " + out);
            }
        }
    }
}
